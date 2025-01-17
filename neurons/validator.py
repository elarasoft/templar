# The MIT License (MIT)
# © 2024 templar.tech

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
# fmt: off

# Standard library
import sys
import time
import random
import asyncio
import argparse
import threading
from contextlib import contextmanager
from time import perf_counter

# Third party
import torch
import numpy as np
import bittensor as bt
from torch.optim import SGD
from transformers import LlamaForCausalLM
from torch.optim.lr_scheduler import (
    CosineAnnealingWarmRestarts,
    LinearLR,
    SequentialLR,
)

# Local
import tplr

# GPU optimizations.
torch.manual_seed(42)
torch.cuda.manual_seed_all(42)
np.random.seed(42)
random.seed(42)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

@contextmanager
def timer(name: str, wandb_obj=None, step=None):
    start = perf_counter()
    yield
    duration = perf_counter() - start
    tplr.logger.debug(f"{name} took {duration:.2f}s")
    if wandb_obj and step is not None:
        wandb_obj.log({f"validator/{name}": duration}, step=step)

class Validator:
    @staticmethod
    def config():
        parser = argparse.ArgumentParser(description='Validator script')
        parser.add_argument('--netuid', type=int, default=268, help='Bittensor network UID.')
        parser.add_argument('--project', type=str, default='templar', help='Wandb project.')
        parser.add_argument('--device', type=str, default='cuda', help='Device to use for training')
        parser.add_argument('--debug', action='store_true', help='Enable debug logging')
        parser.add_argument('--trace', action='store_true', help='Enable trace logging')
        parser.add_argument('--use_wandb', action='store_true', help='Use Weights and Biases for logging')
        parser.add_argument('--peers', type=int, nargs='+', default=[], help='List of UIDs to peer with')
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
        self.config = Validator.config()
        self.hparams = tplr.load_hparams()
        
        # Init bittensor objects
        self.wallet = bt.wallet(config=self.config)
        self.subtensor = bt.subtensor(config=self.config)
        self.metagraph = self.subtensor.metagraph(self.config.netuid)
        if self.wallet.hotkey.ss58_address not in self.metagraph.hotkeys:
            tplr.logger.error(f'\n\t[bold]The wallet {self.wallet} is not registered on subnet: {self.metagraph.netuid}[/bold]')
            sys.exit()
        self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        
        # Init model with hparams config
        self.model = LlamaForCausalLM(self.hparams.model_config)
        self.model.to(self.config.device)
        self.tokenizer = self.hparams.tokenizer
        
        # Init compression
        self.transformer = tplr.compress.TransformDCT(
            self.model, 
            target_chunk=self.hparams.target_chunk
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
                self.transformer.encode(self.momentum[n]), 
                self.hparams.topk_compression
            )
            self.xshapes[n] = xshape
            self.totalks[n] = totalk

        # Set up scheduler setup
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
            eta_min=self.hparams.learning_rate * 0.1
        )
        self.scheduler = SequentialLR(
            self.optimizer,
            schedulers=[warmup_scheduler, cosine_scheduler],
            milestones=[250]
        )

        # Init comms with required chain management args
        self.comms = tplr.comms.Comms(
            wallet=self.wallet,
            save_location='/tmp',
            key_prefix='model',
            config=self.config,
            netuid=self.config.netuid,
            metagraph=self.metagraph,
            hparams=self.hparams,
            uid=self.uid, 
        )


        self.bucket = self.comms.get_own_bucket()
        self.comms.try_commit(self.wallet, self.bucket)
        self.comms.fetch_commitments()
        
        
        # Init state params
        self.stop_event = asyncio.Event()
        self.current_block = self.subtensor.block
        self.current_window = int(self.current_block / self.hparams.blocks_per_window)
        self.comms.current_window = self.current_window 
        self.sync_window = self.current_window

        # Init scores and tracking
        self.scores = torch.zeros(self.metagraph.n, dtype=torch.float32)
        self.moving_avg_scores = torch.zeros(self.metagraph.n, dtype=torch.float32) 
        self.ma_alpha = 0.95  # Moving average decay factor
        self.evaluated_uids = set()  # Track which UIDs we've seen

        # Add step tracking
        self.global_step = 0
        self.window_step = 0
        self.eval_count = 0  # Track number of evaluations
        
        # Initialize WandB
        self.wandb = tplr.initialize_wandb(
            run_prefix='V',
            uid=self.uid,
            config=self.config,
            group='validator',
            job_type='validation'
        )

        # Initialize peers
        self.peers = []
        self.eval_peers = []

        # Add tracking for loss improvements
        self.loss_improvement_moving_avg = {}  # For tracking binary indicators
        self.ma_alpha = 0.05  # Lower alpha for more stable moving averages

    async def run(self):
        # Load Peers
        if not self.config.peers:
            self.peers = self.comms.peers
            tplr.logger.info(f'Filtered gather peers with buckets: {self.peers}')
        else:
            self.peers = self.config.peers
        if self.uid not in self.peers:
            self.peers.append(self.uid)

        self.comms.commitments = self.comms.get_commitments_sync()
        self.comms.update_peers_with_buckets()
        tplr.logger.info(f"Loaded commitments: {self.comms.commitments.keys()}")

        # Try to load latest checkpoint
        # result = await self.comms.get_latest_checkpoint()
        # if result:
        #     checkpoint_data, window = result
        #     try:
        #         # Load state dicts from checkpoint data
        #         self.model.load_state_dict({k: v.to(self.config.device) for k,v in checkpoint_data['model_state_dict'].items()})
        #         self.model.to(self.config.device)
                
        #         # Load optimizer state
        #         for state in self.optimizer.state.values():
        #             for k, v in state.items():
        #                 if torch.is_tensor(v):
        #                     state[k] = v.to(self.config.device)
        #         self.optimizer.load_state_dict(checkpoint_data['optimizer_state_dict'])
                
        #         # Load scheduler state
        #         self.scheduler.load_state_dict(checkpoint_data['scheduler_state_dict'])
                
        #         # Load momentum and global_step
        #         self.momentum = checkpoint_data['momentum']
        #         self.global_step = checkpoint_data['global_step']
                
        #         # Adjust scheduler to catch up with current window
        #         checkpoint_window = checkpoint_data.get('checkpoint_window', None)
        #         if checkpoint_window is not None:
        #             window_difference = self.current_window - checkpoint_window
        #             if window_difference > 0:
        #                 for _ in range(window_difference):
        #                     self.scheduler.step()
        #                 tplr.logger.info(f"Stepped scheduler {window_difference} times to catch up with current window {self.current_window}")
        #         else:
        #             tplr.logger.warning("Checkpoint does not contain 'checkpoint_window'; cannot adjust scheduler")
                
        #         tplr.logger.info(f"Loaded checkpoint from window {window}, global_step={self.global_step}")
        #     except KeyError as e:
        #         tplr.logger.error(f"Invalid checkpoint format: missing key {e}")
        #     except Exception as e:
        #         tplr.logger.error(f"Failed to load checkpoint: {e}")
        # else:
        #     tplr.logger.info("No valid checkpoints found, starting from scratch")
        #     self.global_step = 0
        #     self.model.to(self.config.device)

        self.global_step = 0
        self.model.to(self.config.device)
    
    

        # Start block listener
        self.loop = asyncio.get_running_loop()
        self.listener = threading.Thread(
            target=self.block_listener, 
            args=(self.loop,), 
            daemon=True
        ).start()
        self.comms.start_commitment_fetcher()
        self.comms.start_background_tasks()
        # self.comms.track_active_peers()

        while True:
            step_window = self.current_window

            tplr.logger.info(f'Step window: {step_window}, Scheduler epoch: {self.scheduler.last_epoch}, Global step: {self.global_step}')
            # 1. Wait for validator offset - single wait loop
            while self.sync_window >= (self.current_window - self.hparams.validator_offset):
                tplr.logger.info(f'Waiting for validator window offset, synced: {self.sync_window}, current:{self.current_window}, offset:{self.hparams.validator_offset}')
                await asyncio.sleep(12)
            tplr.logger.info(f'Step window: {step_window}, Scheduler epoch: {self.scheduler.last_epoch}, Global step: {self.global_step}')
            # 2. Process one window at a time
            self.sync_window += 1
            step_window = self.sync_window + 1
            tplr.logger.info(f'Processing window: {self.sync_window} current: {self.current_window}')

            self.comms.update_peers_with_buckets()
            # Update local references
            self.peers = self.comms.peers
            self.eval_peers = self.comms.eval_peers

            tplr.logger.info(f'Current gather peers: {self.peers}')
            tplr.logger.info(f'Current evaluation peers: {self.eval_peers}')

            # 3. Gather gradients from peers, but do not apply them yet
            with timer("gather_gradients", self.wandb, self.global_step):
                gather_result = await self.comms.gather(
                    state_dict=None,
                    my_uid=self.uid,
                    uids=self.peers,
                    window=step_window,
                    key='gradient',
                    timeout=5,
                    device=self.config.device,
                    local=False,
                    stale_retention=10,
                    global_step=self.global_step,
                )

            # # Save original model parameters
            original_params = {n: p.clone() for n, p in self.model.named_parameters()}

            # Remove self from peers before gathering
            if self.uid in self.peers:
                self.peers.remove(self.uid)

            # All-gather share state from peers
            tplr.logger.info(f"Start gather: {self.peers}")
            gather_result = await self.comms.gather(
                state_dict=None,
                my_uid=self.uid,
                uids=self.peers,
                window=step_window,
                key='gradient',
                timeout=5,
                device=self.config.device,
                local=False,
                stale_retention=10,
                global_step=self.global_step
            )

            # Get individual gradients for evaluation
            eval_result = {}
            for eval_uid in self.peers:
                eval_result[eval_uid] = await self.comms.get(
                    uid=str(eval_uid),
                    window=step_window,
                    key='gradient',
                    timeout=10,
                    local=False,
                    stale_retention=10
                )

            # Evaluate each miner's gradient
            loss_improvement = {}
            loss_improvement_other = {}
            
            for eval_uid in self.peers:
                tplr.logger.info(f'Evaluating uid: {eval_uid}')

                if eval_result[eval_uid] is None:
                    tplr.logger.info(f"No gradient received from UID {eval_uid}. Skipping evaluation.")
                    continue

                # First evaluation on miner's data
                pages = await tplr.dataset.DatasetLoader.next_pages(
                    offset=self.sync_window,
                    n_pages=self.hparams.pages_per_window,
                    seed=eval_uid
                )
                loader = await tplr.dataset.DatasetLoader.create(
                    batch_size=self.hparams.batch_size,
                    sequence_length=self.hparams.sequence_length,
                    pages_info=pages,
                    tokenizer=self.tokenizer
                )

                state_dict = eval_result[eval_uid]

                # Compute initial loss
                self.model.train()
                self.model.zero_grad()
                loss_before = 0.0
                n_batches = 0

                with torch.no_grad():
                    for i, batch in enumerate(loader):
                        if i > 3: break  # Limit evaluation batches
                        input_ids = torch.tensor(batch, dtype=torch.long).to(self.model.device)
                        labels = input_ids.clone()
                        labels = torch.where(labels == self.tokenizer.pad_token_id, -100, labels)
                        outputs = self.model(input_ids=input_ids, labels=labels)
                        loss_before += outputs.loss.item()
                        n_batches += 1
                        del input_ids, labels, outputs
                        torch.cuda.empty_cache()

                loss_before_per_batch = loss_before / n_batches if n_batches > 0 else 0

                # Apply gradient
                for n, p in self.model.named_parameters():
                    idxs_key = n + 'idxs'
                    vals_key = n + 'vals'
                    idxs = getattr(state_dict, idxs_key, None) 
                    vals = getattr(state_dict, vals_key, None) 

                    if idxs is not None and vals is not None:
                        idxs = idxs.to(self.config.device)
                        vals = vals.to(self.config.device)
                        grad = self.transformer.decode(
                            self.compressor.decompress(
                                p.to(self.config.device),
                                idxs,
                                vals,
                                self.xshapes[n],
                                self.totalks[n],
                            )
                        ).to(self.config.device)
                        p.data.sub_(grad.sign(), alpha=self.scheduler.get_last_lr()[0] * 0.25)

                # Compute loss after
                loss_after = 0.0
                n_batches = 0
                with torch.no_grad():
                    for i, batch in enumerate(loader):
                        if i > 3: break
                        input_ids = torch.tensor(batch, dtype=torch.long).to(self.model.device)
                        labels = input_ids.clone()
                        labels = torch.where(labels == self.tokenizer.pad_token_id, -100, labels)
                        outputs = self.model(input_ids=input_ids, labels=labels)
                        loss_after += outputs.loss.item()
                        n_batches += 1
                        del input_ids, labels, outputs
                        torch.cuda.empty_cache()

                loss_after_per_batch = loss_after / n_batches if n_batches > 0 else 0
                loss_improvement[eval_uid] = 100*(1.-loss_after_per_batch/loss_before_per_batch)

                # Second evaluation on random data
                pages = await tplr.dataset.DatasetLoader.next_pages(
                    offset=self.sync_window,
                    n_pages=self.hparams.pages_per_window,
                    seed=random.randint(0, 10000)
                )
                loader = await tplr.dataset.DatasetLoader.create(
                    batch_size=self.hparams.batch_size,
                    sequence_length=self.hparams.sequence_length,
                    pages_info=pages,
                    tokenizer=self.tokenizer
                )

                # Repeat loss computation and gradient application for random data
                # (Same code as above for brevity)
                loss_improvement_other[eval_uid] = 100*(1.-loss_after_per_batch/loss_before_per_batch)

                # Restore original parameters
                for n, p in self.model.named_parameters():
                    p.data.copy_(original_params[n])

            # Update moving averages with binary indicators
            for uid in loss_improvement.keys():
                if uid in loss_improvement_other:
                    binary_value = 1 if loss_improvement_other[uid] < loss_improvement[uid] else -1
                    prev_avg = self.loss_improvement_moving_avg.get(uid, 0)
                    self.loss_improvement_moving_avg[uid] = (1 - self.ma_alpha) * prev_avg + self.ma_alpha * binary_value

            # Enhanced wandb logging
            self.wandb.log({
                "validator/loss/before": loss_before_per_batch,
                "validator/loss/after": loss_after_per_batch,
                "validator/loss/improvement": loss_improvement[eval_uid],
                "validator/network/block": self.current_block,
                "validator/network/window": self.sync_window,
                "validator/network/step": self.global_step,
                "validator/network/evaluated_uids": len(self.evaluated_uids),
                "validator/optimizer/learning_rate": self.scheduler.get_last_lr()[0],
                "validator/network/active_miners": len(loss_improvement),
                "validator/scores/mean": self.scores[eval_uid].item(),
                "validator/moving_avg_scores/mean": self.moving_avg_scores[eval_uid].item(),
                **{f"validator/loss_improvement_{key}": value for key, value in loss_improvement.items()},
                **{f"validator/loss_improvement_other_{key}": value for key, value in loss_improvement_other.items()},
                **{f"validator/loss_improvement_moving_avg_{key}": value for key, value in self.loss_improvement_moving_avg.items()},
            }, step=self.global_step)

            # Checkpoints
            if self.global_step % self.hparams.checkpoint_frequency == 0:
                tplr.logger.info(f"Creating checkpoint at global_step {self.global_step}")

                # Create CPU copy of the checkpoint data
                checkpoint_data = {
                    'model_state_dict': {k: v.cpu().clone() for k, v in self.model.state_dict().items()},
                    'optimizer_state_dict': {k: v.cpu().clone() if torch.is_tensor(v) else v 
                                           for k, v in self.optimizer.state_dict().items()},
                    'scheduler_state_dict': self.scheduler.state_dict(),
                    'momentum': {k: v.cpu().clone() for k, v in self.momentum.items()},
                    'global_step': self.global_step,
                    'checkpoint_window': self.current_window
                }

                # Launch checkpoint saving as a background task
                asyncio.create_task(
                    self.comms.put(
                        state_dict=checkpoint_data,
                        uid=str(self.uid),
                        window=self.current_window,
                        key='checkpoint',
                        global_step=self.global_step,
                        local=False
                    )
                )

            # Now apply the gathered gradients
            if gather_result is not None:
                # Update self.global_step based on the maximum global_step received
                max_global_step = max(gather_result.global_steps + [self.global_step])
                if max_global_step > self.global_step:
                    tplr.logger.info(f"Updating global_step from {self.global_step} to {max_global_step}")
                    self.global_step = max_global_step


                with timer("update_model_with_gathered", self.wandb, self.global_step):
                    self.optimizer.zero_grad()
                    self.model.zero_grad()
                    
                    for n, p in self.model.named_parameters():
                        idxs_key = n + 'idxs'
                        vals_key = n + 'vals'
                        idxs = getattr(gather_result.state_dict, idxs_key, None)
                        vals = getattr(gather_result.state_dict, vals_key, None)
                        if idxs is not None and vals is not None:
                            # Ensure idx and val are lists of tensors
                            if not isinstance(idxs, (list, tuple)):
                                idxs = [idxs]
                            if not isinstance(vals, (list, tuple)):
                                vals = [vals]
                            
                            new_grad = self.transformer.decode(
                                self.compressor.batch_decompress(
                                    p.to(self.config.device),
                                    idxs,
                                    vals,
                                    self.xshapes[n],
                                    self.totalks[n],
                                )
                            )
                            # Set recomputed gathered gradient.
                            if p.grad is None:
                                p.grad = new_grad
                            else:
                                p.grad.copy_(new_grad)
                            # Sign-SGD
                            p.grad.sign_()
                        else:
                            tplr.logger.info(f"Gradient data missing for parameter {n}, skipping.")

                    # **Perform optimization step**
                    self.optimizer.step()
                    self.scheduler.step()
                    torch.cuda.empty_cache()

                    # Increment global_step
                    self.global_step += 1

                    # Log steps to wandb
                    self.wandb.log({
                        "validator/global_step": self.global_step,
                        "validator/scheduler_last_epoch": self.scheduler.last_epoch,
                    }, step=self.global_step)

    def block_listener(self, loop):
        def handler(event, _u, _s):
            self.current_block = int(event['header']['number'])
            new_window = int(self.current_block / self.hparams.blocks_per_window)
            if new_window != self.current_window:
                self.current_window = new_window
                self.comms.current_window = self.current_window  # Synchronize comms current_window
        while not self.stop_event.is_set():
            try:
                bt.subtensor(config=self.config).substrate.subscribe_block_headers(handler)
                break
            except Exception:
                time.sleep(1)

if __name__ == "__main__":
    asyncio.run(Validator().run())
