# The MIT License (MIT)
# © 2025 tplr.ai

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


# Standard library
from datetime import datetime, timedelta, timezone
import sys
import time
import random
import asyncio
import argparse
import threading
from typing import cast

# Third party
from bittensor.core.subtensor import ScaleObj
import torch
import numpy as np
import bittensor as bt
from torch.optim import SGD
from torch import autocast
from transformers import LlamaForCausalLM
from torch.optim.lr_scheduler import (
    CosineAnnealingWarmRestarts,
    LinearLR,
    SequentialLR,
)

# Local
import tplr


# GPU optimizations
torch.manual_seed(42)
torch.cuda.manual_seed_all(42)
np.random.seed(42)
random.seed(42)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True


class Miner:
    # Command line config items.
    @staticmethod
    def config():
        parser = argparse.ArgumentParser(description="Miner script")
        parser.add_argument(
            "--netuid", type=int, default=268, help="Bittensor network UID."
        )
        parser.add_argument(
            "--project", type=str, default="templar", help="Wandb project."
        )
        parser.add_argument(
            "--device", type=str, default="cuda", help="Device to use for training"
        )
        parser.add_argument("--debug", action="store_true", help="Enable debug logging")
        parser.add_argument("--trace", action="store_true", help="Enable trace logging")
        parser.add_argument(
            "--store-gathers",
            action="store_true",
            help="Store gathered gradients in R2",
        )
        parser.add_argument(
            "--test",
            action="store_true",
            help="Test mode - use all peers without filtering",
        )
        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        config = bt.config(parser)
        if config.debug:
            tplr.debug()
        if config.trace:
            tplr.trace()
        return config

    def __init__(self):
        tplr.logger.debug("Starting initialization...")

        # Init config and load hparams
        self.config = Miner.config()
        self.hparams = tplr.load_hparams()

        # Init bittensor objects
        self.wallet = bt.wallet(config=self.config)
        self.subtensor = bt.subtensor(config=self.config)
        self.metagraph = self.subtensor.metagraph(cast(int, self.config.netuid))
        if self.wallet.hotkey.ss58_address not in self.metagraph.hotkeys:
            tplr.logger.error(
                f"\n\t[bold]The wallet {self.wallet} is not registered on subnet: {self.metagraph.netuid}[/bold]"
            )
            sys.exit()
        self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)

        # Init model with hparams config
        self.model = LlamaForCausalLM(self.hparams.model_config)
        self.model.to(self.config.device)  # type: ignore
        self.tokenizer = self.hparams.tokenizer

        # Init compression
        self.transformer = tplr.compress.TransformDCT(
            self.model, target_chunk=self.hparams.target_chunk
        )
        self.compressor = tplr.compress.CompressDCT()

        # Init optimizer and momentum
        self.optimizer = SGD(self.model.parameters(), lr=self.hparams.learning_rate)
        self.momentum = {}
        self.xshapes = {}
        self.totalks = {}
        for n, p in self.model.named_parameters():
            self.momentum[n] = torch.zeros_like(p)
            _, _, xshape, totalk = self.compressor.compress(
                self.transformer.encode(self.momentum[n]), self.hparams.topk_compression
            )
            self.xshapes[n] = xshape
            self.totalks[n] = totalk
        # Set up scheduler
        warmup_scheduler = LinearLR(
            self.optimizer,
            start_factor=0.1,
            end_factor=1.0,
            total_iters=250,
        )
        cosine_scheduler = CosineAnnealingWarmRestarts(
            self.optimizer,
            T_0=10000,
            T_mult=2,
            eta_min=self.hparams.learning_rate * 0.1,
        )
        self.scheduler = SequentialLR(
            self.optimizer,
            schedulers=[warmup_scheduler, cosine_scheduler],
            milestones=[250],
        )

        # Init comms
        self.comms = tplr.comms.Comms(
            wallet=self.wallet,
            config=self.config,
            metagraph=self.metagraph,
            hparams=self.hparams,
            uid=self.uid,
        )

        self.bucket = self.comms.bucket

        self.peer_manager = tplr.PeerManager(
            chain=self.comms, hparams=self.hparams, metagraph=self.metagraph
        )
        self.chain_sync = tplr.ChainSync(
            config=self.config,
            netuid=cast(int, self.config.netuid),
            metagraph=self.metagraph,
            hparams=self.hparams,
            wallet=self.wallet,
        )

        # Init state params
        self.stop_event = asyncio.Event()
        self.current_block = self.subtensor.block
        self.current_window = int(self.current_block / self.hparams.blocks_per_window)
        self.start_window = self.current_window  # Record the start window
        self.global_step = 0
        self.step_counter = 0

        # Add step tracking
        self.window_step = 0

        # Track additional metrics
        self.total_tokens_processed = 0
        self.batch_times = []  # For tracking processing speed

        # Initialize WandB
        self.wandb = tplr.initialize_wandb(
            run_prefix="M",
            uid=self.uid,
            config=self.config,
            group="miner",
            job_type="mining",
        )

    # Main training loop.
    async def run(self):
        # Start background block listener
        self.loop = asyncio.get_running_loop()
        self.listener = threading.Thread(
            target=self.block_listener,
            args=(self.loop,),
            daemon=True,
        )
        self.listener.start()  #

        # await self.chain_sync.get_commitments()

        # Fetch start_window from highest stake validator
        self.start_window = await self.comms.get_start_window()
        tplr.logger.info(f"Using start_window: {self.start_window}")

        self.global_step = self.current_window - self.start_window
        tplr.logger.info(f"starting at Global Step : {self.global_step}")

        # Proceed to load checkpoint
        (
            success,
            loaded_momentum,
            loaded_checkpoint_window,
            loaded_optimizer,
            loaded_scheduler,
        ) = await self.comms.load_checkpoint(
            model=self.model,
            optimizer=self.optimizer,
            scheduler=self.scheduler,
            device=cast(str, self.config.device),
        )
        if (
            success
            and loaded_momentum is not None
            and loaded_momentum is not None
            and loaded_optimizer is not None
            and loaded_scheduler is not None
            and loaded_checkpoint_window is not None
        ):
            self.momentum = loaded_momentum
            self.global_step = loaded_checkpoint_window - self.start_window
            self.optimizer = loaded_optimizer
            self.scheduler = loaded_scheduler
            tplr.logger.info(
                f"Loaded checkpoint with global_step={self.global_step}, "
                f"optimizer_step={self.optimizer.state_dict()['state'].get(0, {}).get('step', 0)}, "
                f"scheduler_step={self.scheduler.last_epoch}"
            )
            # Only catch up if we're behind
            if loaded_checkpoint_window < self.current_window:
                tplr.logger.info(
                    f"Checkpoint is behind current window ({loaded_checkpoint_window} < {self.current_window}), starting catchup..."
                )
                await self.catchup_with_aggregation_server(loaded_checkpoint_window)
            else:
                tplr.logger.info("Checkpoint is up-to-date, skipping catchup.")
        else:
            tplr.logger.info("Starting from scratch")
            self.momentum = {
                n: torch.zeros_like(p) for n, p in self.model.named_parameters()
            }
            self.model.to(self.config.device)  # type: ignore

        while True:
            # 1. Initialize window and update peers
            window_start = tplr.T()
            # Start the gather in the background:
            gather_start = tplr.T()
            step_window = self.current_window
            self.global_step = (
                self.current_window - self.start_window
            )  # Update global_step
            tplr.logger.info(
                f"\n{'-' * 40} Window: {step_window} (Global Step: {self.global_step}) {'-' * 40}"
            )

            peer_start = tplr.T()
            self.peers = self.chain_sync.set_gather_peers()
            # self.peers = self.chain_sync.peers
            tplr.logger.info(
                f"{tplr.P(step_window, tplr.T() - peer_start)} Updated peers - gather:{len(self.peers)}"
            )

            # 2. Load training data for this window
            data_start = tplr.T()
            pages = await tplr.r2_dataset.R2DatasetLoader.next_pages(
                offset=step_window,
                n_pages=self.hparams.pages_per_window,
                seed=self.uid,  # type: ignore
            )
            loader = await tplr.r2_dataset.R2DatasetLoader.create(
                batch_size=self.hparams.batch_size,
                sequence_length=self.hparams.sequence_length,
                pages_info=pages,
                tokenizer=self.tokenizer,
            )
            tplr.logger.info(
                f"{tplr.P(step_window, tplr.T() - data_start)} Loaded training data"
            )
            tplr.logger.info(
                f"Pages: {[p[1] for p in pages]} for  Window: {step_window}"
            )  # type: ignore

            # 3. Accumulate gradients over batches
            train_start = tplr.T()
            tplr.logger.info("Start accumulating...")
            self.optimizer.zero_grad()
            self.model.zero_grad()
            total_loss = 0.0
            n_batches = 0

            for i, batch in enumerate(loader):
                input_ids = torch.tensor(batch, dtype=torch.long).to(self.model.device)
                labels = input_ids.clone()
                labels = torch.where(
                    labels == self.tokenizer.pad_token_id, -100, labels
                )

                with autocast(device_type=self.model.device.type, dtype=torch.bfloat16):
                    outputs = self.model(input_ids=input_ids, labels=labels)

                total_loss += outputs.loss.item()
                outputs.loss.backward()
                n_batches += 1
                tplr.logger.info(f"loss: {outputs.loss.item()} [Batch {i + 1}]")
                if self.current_window != step_window:
                    tplr.logger.info("<Exhausted window>")
                    break

            # If training completes before the window is exhausted, wait until the window ends.
            if self.current_window == step_window:
                tplr.logger.info(
                    "Training complete; waiting for window to be exhausted..."
                )
                while self.current_window == step_window:
                    await asyncio.sleep(
                        0.1
                    )  # TODO: Consider adding a timeout safeguard here.
            tplr.logger.info(
                f"{tplr.P(step_window, tplr.T() - train_start)} Completed training"
            )

            compress_start = tplr.T()
            gradient, _, _, _ = tplr.prepare_gradient_dict(self, pages, step_window)
            tplr.logger.info(
                f"{tplr.P(step_window, tplr.T() - compress_start)} Compressed local gradients"
            )
            tplr.logger.debug(f"Putting own state dict for UID {self.uid}")

            # Move everything to CPU before upload
            processed_state_dict = {}
            for k, v in gradient.items():
                if isinstance(v, torch.Tensor):
                    processed_state_dict[k] = v.to("cpu")
                else:
                    processed_state_dict[k] = v

            # Launch the put operation as a background task
            put_completion_time = await self.comms.put(
                state_dict=processed_state_dict,
                uid=str(self.uid),
                window=step_window,
                key="gradient",
                global_step=self.global_step,
                local=False,
                stale_retention=100,
            )

            upload_size = sum(
                tensor.element_size() * tensor.nelement()
                for tensor in processed_state_dict.values()
                if isinstance(tensor, torch.Tensor)
            )
            tplr.logger.info(
                f"Uploading {upload_size} bytes of own state for UID: {self.uid}"
            )

            tplr.logger.info(f"Stopped accumulating: {n_batches} batches")

            sync_block = self.current_window * self.hparams.blocks_per_window
            retries = 0
            delay = 1
            max_retries = 5
            max_delay = 60
            while True:
                try:
                    response = self.subtensor.query_module(
                        "Timestamp", "Now", block=sync_block
                    )
                    if response is None or not isinstance(response, ScaleObj):
                        raise ValueError(f"Could not query timestamp for {sync_block}")
                    ts_value = (
                        cast(int, response.value) / 1000
                    )  # convert milliseconds to seconds
                    break
                except Exception as e:
                    tplr.logger.error(
                        f"Failed to query timestamp for block {sync_block}: {str(e)}. Retry {retries + 1}/{max_retries}"
                    )
                    retries += 1
                    if retries > max_retries:
                        tplr.logger.error(
                            "Exceeded maximum retries for timestamp query."
                        )
                        raise e
                    time.sleep(delay)
                    delay = min(delay * 2, max_delay)

            time_min = datetime.fromtimestamp(ts_value, tz=timezone.utc)
            time_max = time_min + timedelta(
                seconds=self.hparams.time_window_delta_seconds
            )

            # Log the time window we're using
            tplr.logger.info(f"Using time window for gather: {time_min} to {time_max}")

            # Refresh the peers list immediately before gathering
            tplr.logger.info("Refreshing peers before gather task...")

            tplr.logger.info(
                f"Final peers for gather: {self.peer_manager.active_peers}"
            )

            tplr.logger.info(f"Gathering gradients from peers: {self.peers}")
            gather_task = asyncio.create_task(
                self.comms.gather(
                    my_uid=self.uid,
                    uids=self.peers,
                    window=step_window,
                    key="gradient",
                    timeout=35,
                    device="cpu",
                    local=False,
                    stale_retention=100,
                    time_min=time_min,
                    time_max=time_max,
                )
            )
            gather_result = await gather_task

            if gather_result is None:
                tplr.logger.error(
                    "Failed to gather gradients from peers. Waiting for next window."
                )
                while self.current_window == step_window:
                    await asyncio.sleep(0.1)
                continue

            # --- Validate gathered gradients from peers ---
            valid_uids = []
            invalid_uids = []
            for uid in gather_result.uids:
                peer_state = gather_result.state_dicts.get(uid, {})
                is_valid, err_msg = tplr.neurons.validate_compressed_gradients(
                    peer_state,
                    self.totalks,
                    allowed_topk=self.hparams.topk_compression,
                    device=self.config.device,
                )
                if is_valid:
                    valid_uids.append(uid)
                else:
                    tplr.logger.warning(
                        f"Gradient from UID {uid} failed validation: {err_msg}"
                    )
                    invalid_uids.append(uid)
            gather_result.uids = valid_uids
            gather_result.skipped_uids.extend(invalid_uids)
            # TODO: Add error handling here if gather_result.state_dicts is empty or missing keys.

            # 5. Calculate and log metrics
            duration = time.time() - train_start
            self.batch_times.append(duration)
            self.total_tokens_processed += n_batches

            grad_norms = [
                p.grad.norm().item()
                for p in self.model.parameters()
                if p.grad is not None
            ]
            weight_norms = [p.norm().item() for p in self.model.parameters()]
            momentum_norms = [m.norm().item() for m in self.momentum.values()]
            self.wandb.log(
                {
                    # Training metrics
                    "miner/loss": total_loss / n_batches if n_batches > 0 else 0,
                    "miner/tokens_per_sec": n_batches / duration,
                    "miner/batch_duration": duration,
                    "miner/total_tokens": self.total_tokens_processed,
                    "miner/batch_tokens": n_batches,
                    "miner/global_step": self.global_step,
                    # Resource metrics
                    "miner/gpu_memory_allocated": torch.cuda.memory_allocated()
                    / 1024**2,  # MB
                    "miner/gpu_memory_cached": torch.cuda.memory_reserved()
                    / 1024**2,  # MB
                    # Network metrics
                    "miner/gather_peers": len(self.peers),
                    "miner/effective_batch_size": len(self.peers)
                    * self.hparams.batch_size,
                    # Optimization metrics
                    "miner/learning_rate": self.scheduler.get_last_lr()[0],
                    # Gradient statistics as points
                    "miner/mean_grad_norm": sum(grad_norms) / len(grad_norms)
                    if grad_norms
                    else 0,
                    "miner/max_grad_norm": max(grad_norms) if grad_norms else 0,
                    "miner/min_grad_norm": min(grad_norms) if grad_norms else 0,
                    "miner/grad_norm_std": torch.tensor(grad_norms).std().item()
                    if grad_norms
                    else 0,
                    "miner/mean_weight_norm": sum(weight_norms) / len(weight_norms),
                    "miner/mean_momentum_norm": sum(momentum_norms)
                    / len(momentum_norms),
                },
                step=self.global_step,
            )

            # ---------------------------------------------------------------------
            # 6. Await both gather
            # ---------------------------------------------------------------------

            tplr.logger.info("Put task completed!")

            # tplr.logger.info("Waiting on gather task...")
            # gather_result = await gather_task
            # tplr.logger.info("Gather task completed!")

            # if gather_result is None:
            #     tplr.logger.error(
            #         "Failed to gather gradients from peers. Waiting for next window."
            #     )
            #     while self.current_window == step_window:
            #         await asyncio.sleep(0.1)
            #     continue

            # 8. Apply gathered gradients
            update_start = tplr.T()
            for n, p in self.model.named_parameters():
                idxs_key = n + "idxs"
                vals_key = n + "vals"
                all_idxs = []
                all_vals = []
                # Aggregate gradients from all valid peers
                for uid, state in gather_result.state_dicts.items():
                    if idxs_key in state and vals_key in state:
                        idx = state[idxs_key]
                        val = state[vals_key]
                        if not isinstance(idx, (list, tuple)):
                            idx = [idx]
                        if not isinstance(val, (list, tuple)):
                            val = [val]
                        all_idxs.extend(idx)
                        all_vals.extend(val)
                if all_idxs and all_vals:
                    new_grad = self.transformer.decode(
                        self.compressor.batch_decompress(
                            p.to(self.config.device),
                            all_idxs,
                            all_vals,
                            self.xshapes[n],
                            self.totalks[n],
                        )
                    )
                    p.data.sub_(new_grad.sign(), alpha=self.scheduler.get_last_lr()[0])
                else:
                    tplr.logger.info(
                        f"Gradient data missing for parameter {n}, skipping."
                    )
            tplr.logger.info(
                f"{tplr.P(step_window, tplr.T() - update_start)} Updated model"
            )

            # 10. Optimization step
            tplr.logger.info("Finish and step.")
            self.optimizer.step()
            self.scheduler.step()

            # Log total window time and add timing metrics to existing wandb logging
            tplr.logger.info(
                f"{tplr.P(step_window, tplr.T() - window_start)} Completed window iteration"
            )

            # Add debug data including successfully gathered peers
            debug_dict = {}

            # Add model parameters debug info
            for name, param in self.model.named_parameters():
                if (
                    param is not None and param.numel() >= 2
                ):  # Check if tensor has at least 2 elements
                    debug_dict[name + "_debug"] = (
                        param.flatten()[:2].detach().cpu().tolist()
                    )

            # Add successful peers information
            if gather_result is not None:
                debug_dict["successful_peers"] = sorted(
                    list(set(self.peers) - set(gather_result.skipped_uids))
                )
                debug_dict["skipped_peers"] = sorted(list(gather_result.skipped_uids))

            # Store the debug dictionary
            asyncio.create_task(
                self.comms.put(
                    state_dict=debug_dict,
                    uid=str(self.uid),
                    window=step_window,
                    key="debug",
                    local=False,
                )
            )
            tplr.logger.info(f"Stored debug values for window {self.current_window}")
            # Log total window time and metrics
            tplr.logger.info(
                f"{tplr.P(self.current_window, tplr.T() - window_start)} Completed window iteration"
            )

            self.wandb.log(
                {
                    # Add timing metrics
                    "miner/timing/window_total": tplr.T() - window_start,
                    "miner/timing/peer_update": tplr.T() - peer_start,
                    "miner/timing/data_loading": tplr.T() - data_start,
                    "miner/timing/training": tplr.T() - train_start,
                    "miner/timing/compression": tplr.T() - compress_start,
                    "miner/timing/gather": tplr.T() - gather_start,
                    "miner/timing/put": put_completion_time,
                    "miner/timing/model_update": tplr.T() - update_start,
                    # Existing metrics
                    "miner/loss": total_loss / n_batches if n_batches > 0 else 0,
                    "miner/tokens_per_sec": n_batches / duration,
                    "miner/total_tokens": self.total_tokens_processed,
                    "miner/batch_tokens": n_batches,
                    "miner/global_step": self.global_step,
                    "miner/gpu_memory_allocated": torch.cuda.memory_allocated()
                    / 1024**2,  # MB
                    "miner/gpu_memory_cached": torch.cuda.memory_reserved()
                    / 1024**2,  # MB
                    "miner/gather_peers": len(self.peers),
                    "miner/effective_batch_size": len(self.peers)
                    * self.hparams.batch_size,
                    "miner/learning_rate": self.scheduler.get_last_lr()[0],
                    "miner/mean_grad_norm": sum(grad_norms) / len(grad_norms)
                    if grad_norms
                    else 0,
                    "miner/max_grad_norm": max(grad_norms) if grad_norms else 0,
                    "miner/min_grad_norm": min(grad_norms) if grad_norms else 0,
                    "miner/grad_norm_std": torch.tensor(grad_norms).std().item()
                    if grad_norms
                    else 0,
                    "miner/mean_weight_norm": sum(weight_norms) / len(weight_norms),
                    "miner/mean_momentum_norm": sum(momentum_norms)
                    / len(momentum_norms),
                    # Added gather success rate in %
                    "miner/gather/success_rate": gather_result.success_rate * 100
                    if gather_result
                    else 0,
                },
                step=self.global_step,
            )

            self.global_step += 1
            self.window_step += 1
            tplr.logger.info(f"Total optimization steps: {self.global_step}")

            # Save checkpoint logic
            if self.global_step % self.hparams.checkpoint_frequency == 0:
                tplr.logger.info(
                    f"Creating checkpoint at global_step {self.global_step}"
                )

                # asyncio checkpoint saving task
                asyncio.create_task(
                    self.comms.save_remote_checkpoint(
                        model=self.model,
                        optimizer=self.optimizer,
                        scheduler=self.scheduler,
                        momentum=self.momentum,
                        current_window=self.current_window,
                        start_window=self.start_window,
                    )
                )
            else:
                tplr.logger.info("Skipping checkpoint save this round")

            # 4. Wait for next window
            tplr.logger.info("Wait for next window...")
            while self.current_window == step_window:
                await asyncio.sleep(0.1)

    async def catchup_with_aggregation_server(self, checkpoint_current_window):
        """
        Catch up the model by applying aggregated gradients from the aggregation server
        and verifying against the validator's debug dict.
        """
        tplr.logger.info("Starting catchup with aggregation server...")

        # Start from the checkpoint window and continue until we reach the current window
        checkpoint_window = checkpoint_current_window + 1
        target_window = self.current_window

        tplr.logger.info(
            f"Catching up from window {checkpoint_window} to current window {target_window}"
        )

        weight_decay = self.hparams.weight_decay

        # Apply aggregation for each step, checking for current window changes
        current_step = checkpoint_window
        while current_step <= target_window:
            # Check if current_window has changed during processing
            if self.current_window > target_window:
                target_window = self.current_window
                tplr.logger.info(
                    f"Current window advanced during catchup, new target: {target_window}"
                )

            tplr.logger.info(
                f"\nProcessing catchup for window {current_step} (Target: {target_window})"
            )

            # Load aggregation for current window - pass version explicitly
            agg_data = await self.comms.load_aggregation(window=current_step)
            if not agg_data:
                tplr.logger.warning(
                    f"No aggregation data found for window {current_step}, skipping"
                )
                current_step += 1
                continue

            processed_agg_data = self.process_loaded_data(agg_data)

            # Get learning rate for this step
            lr = self.get_lr_at_step(current_step - self.start_window)
            tplr.logger.info(f"Using learning rate: {lr:.6f} for catchup step")

            # Get debug dictionary for verification
            debug_dict = await self.comms.get_debug_dict(current_step - 1)

            # Apply aggregation to model
            tplr.logger.info(
                f"Applying aggregation to model for window {current_step}..."
            )
            with torch.no_grad():
                for name, param in self.model.named_parameters():
                    if name in processed_agg_data["tensors"]:
                        # Move aggregation tensor to device
                        agg_tensor = processed_agg_data["tensors"][name].to(
                            cast(str, self.config.device)
                        )

                        # Apply weight decay to parameter
                        param.data.mul_(1.0 - lr * weight_decay)

                        # Apply gradient update with learning rate
                        param.data.add_(agg_tensor, alpha=-lr)

            tplr.logger.info(
                f"Successfully applied aggregation for window {current_step}"
            )

            # Calculate L2 norm of difference between model and debug values after this step
            if isinstance(debug_dict, dict) and "state_dict" in debug_dict:
                debug_state_dict = debug_dict["state_dict"]
                total_squared_diff = 0.0
                param_count = 0
                abs_diff = 0

                for name, param in self.model.named_parameters():
                    # Check if there's a corresponding debug entry
                    debug_key = name + "_debug"
                    if debug_key in debug_state_dict:
                        # Calculate L2 norm for this parameter
                        param_data = param.data.cpu().flatten()[
                            :2
                        ]  # Take only first two values
                        debug_data = torch.tensor(
                            cast(float, debug_state_dict[debug_key])
                        ).cpu()
                        squared_diff = torch.sum((param_data - debug_data) ** 2).item()
                        total_squared_diff += squared_diff
                        abs_diff += (
                            torch.abs(torch.tensor(param_data - debug_data))
                            .mean()
                            .item()
                        )
                        param_count += param_data.numel()

                # Final L2 norm across all parameters
                final_l2_norm = torch.sqrt(torch.tensor(total_squared_diff)).item()
                tplr.logger.info(
                    f"Window {current_step} - L2 norm difference between model and debug values: {final_l2_norm}"
                )
                tplr.logger.info(
                    f"Window {current_step} - Average L2 norm per parameter: {final_l2_norm / param_count if param_count > 0 else 0}"
                )
                tplr.logger.info(
                    f"Window {current_step} - Average absolute difference per parameter: {abs_diff / param_count / lr if param_count > 0 else 0}"
                )
            else:
                tplr.logger.info(
                    f"No valid debug dictionary available for window {current_step - 1} - cannot compute L2 norm"
                )

            # Update global step and move to next window
            self.global_step = current_step - self.start_window
            current_step += 1

            # Step optimizer and scheduler to match the current window
            self.optimizer.step()
            self.scheduler.step()

        # Update global step after catchup
        self.global_step = target_window - self.start_window
        tplr.logger.info(f"Catchup complete. Global step updated to {self.global_step}")

    def process_loaded_data(self, compressed_data):
        """
        Unpack the compressed tensor data from the aggregation server.

        Args:
            compressed_data: The compressed tensor data

        Returns:
            Dictionary with unpacked tensors
        """
        result = {
            "timestamp": compressed_data.get("timestamp", None),
            "window": compressed_data.get("window", None),
            "version": compressed_data.get("version", None),
            "tensors": {},
        }

        for name, param in self.model.named_parameters():
            if name in compressed_data:
                original_shape = param.shape
                # Use unpack_binary_tensor from the sample, but in our context
                unpacked = self.unpack_binary_tensor(
                    compressed_data[name], original_shape
                )
                result["tensors"][name] = unpacked
                tplr.logger.debug(f"Unpacked tensor {name} with shape {original_shape}")

        tplr.logger.info(f"Successfully unpacked {len(result['tensors'])} tensors")
        return result

    def unpack_binary_tensor(self, packed_tensor, original_shape):
        """
        Unpack a 1-bit representation tensor back to ±1 values.

        Args:
            packed_tensor: The packed binary tensor
            original_shape: The original shape of the tensor

        Returns:
            Unpacked tensor with original shape
        """
        total_elements = int(torch.prod(torch.tensor(original_shape)).item())

        # Create a flat tensor to hold the unpacked values
        unpacked = torch.zeros(total_elements, dtype=torch.float32)

        for i in range(8):
            mask = 1 << i
            bits = (packed_tensor & mask) >> i
            # Convert 0/1 to -1/+1
            unpacked[i::8] = (bits.float() * 2) - 1

        return unpacked.reshape(original_shape)

    def get_lr_at_step(self, global_step):
        """
        Get the learning rate at a specific global step.

        Args:
            global_step: The global step to calculate LR for

        Returns:
            Learning rate value
        """
        # Create temporary objects to avoid modifying originals
        temp_optimizer = SGD([torch.tensor([1.0])], lr=self.hparams.learning_rate)

        # Recreate the schedulers to match our existing scheduler configuration
        temp_warmup_scheduler = LinearLR(
            temp_optimizer,
            start_factor=0.1,
            end_factor=1.0,
            total_iters=250,
        )
        temp_cosine_scheduler = CosineAnnealingWarmRestarts(
            temp_optimizer,
            T_0=10000,
            T_mult=2,
            eta_min=self.hparams.learning_rate * 0.1,
        )
        temp_scheduler = SequentialLR(
            temp_optimizer,
            schedulers=[temp_warmup_scheduler, temp_cosine_scheduler],
            milestones=[250],
        )

        # Step to the target global step
        for _ in range(global_step):
            temp_scheduler.step()

        # Return the current learning rate
        return temp_optimizer.param_groups[0]["lr"]

    # Listens for new blocks and sets self.current_block and self.current_window
    def block_listener(self, _):
        import websockets.exceptions  # Ensure we catch websockets errors

        def handler(event):
            try:
                self.current_block = int(event["header"]["number"])
                new_window = int(self.current_block / self.hparams.blocks_per_window)
                if new_window != self.current_window:
                    self.current_window = new_window
                    tplr.logger.info(
                        f"New block received. Current window updated to: {self.current_window}"
                    )
            except Exception as e:
                tplr.logger.error(f"Error processing block event: {e}")

        backoff = 1  # initial backoff in seconds
        max_backoff = 60  # maximum backoff limit

        while not self.stop_event.is_set():
            try:
                # This call subscribes to block headers and might throw keepalive errors
                bt.subtensor(config=self.config).substrate.subscribe_block_headers(
                    handler
                )
                backoff = 1  # reset backoff if subscription exits without exception
            except websockets.exceptions.ConnectionClosedError as e:
                tplr.logger.warning(
                    f"Websocket ConnectionClosedError caught: {e}. Retrying in {backoff} seconds."
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except Exception as e:
                tplr.logger.error(
                    f"Block subscription error: {e}. Retrying in {backoff} seconds."
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)


# Start miner.
if __name__ == "__main__":
    asyncio.run(Miner().run())
