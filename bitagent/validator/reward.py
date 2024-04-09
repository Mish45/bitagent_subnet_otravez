# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 RogueTensor

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import time
import torch
import shutil
import asyncio
import bittensor as bt
from typing import List
from rich.console import Console
from bitagent.protocol import QnAResult
from bitagent.validator.tasks import Task
from common.base.validator import BaseValidatorNeuron

rich_console = Console()
os.environ["WANDB_SILENT"] = "true"
                
async def log_to_wandb(response, validator, wandb_basics, task_results, miner_uid, score, max_possible_score, normalized_score, correct_answer):
    vwandb = None
    # only log to wandb if the response is successful
    if (response.axon.status_code == 200 or response.dendrite.status_code == 200):
        vwandb = validator.init_wandb(miner_uid, wandb_basics['validator_uid'])
    if vwandb:
        step_log = {
            "completion": response.response,
            "correct_answer": correct_answer,
            "miner_uid": miner_uid,
            "score": score,
            "max_possible_score": max_possible_score,
            "normalized_score": normalized_score,
            "average_score_for_miner_with_this_validator": validator.scores[miner_uid],
            "highest_score_for_miners_with_this_validator": validator.scores.max(),
            "median_score_for_miners_with_this_validator": validator.scores.median(),
            "stake": validator.metagraph.S[miner_uid].item(),
            "trust": validator.metagraph.T[miner_uid].item(),
            "incentive": validator.metagraph.I[miner_uid].item(),
            "consensus": validator.metagraph.C[miner_uid].item(),
            "dividends": validator.metagraph.D[miner_uid].item(),
            "task_results": "\n".join(task_results),
            "dendrite_process_time": response.dendrite.process_time,
            "dendrite_status_code": response.dendrite.status_code,
            "axon_status_code": response.axon.status_code,
            "val_spec_version": validator.spec_version,
            **wandb_basics
        }
        vwandb.log(step_log)
        try:
            #bt.logging.debug("Writing to wandb and cleaning up.")
            vwandb.finish()
            wandb_dir_to_delete = vwandb.dir
            if "files" in wandb_dir_to_delete:
                wandb_dir_to_delete = wandb_dir_to_delete.split("files")[0]
            if wandb_dir_to_delete and os.path.exists(wandb_dir_to_delete):
                shutil.rmtree(wandb_dir_to_delete)
            # if it still exists - yikes
            if os.path.exists(wandb_dir_to_delete):
                bt.logging.error(f"Failed to delete wandb directory, you can manually remove: {wandb_dir_to_delete}.")
        except Exception as e:
            bt.logging.error(f"Failed to delete wandb directory. Error: {e}.")

async def process_rewards_update_scores_and_send_feedback(validator: BaseValidatorNeuron, task: Task, responses: List[str], 
                miner_uids: List[int]) -> None:
    """
    Returns a tensor of rewards for the given query and responses.

    Args:
    - task (Task): The task sent to the miner.
    - responses (List[float]): A list of responses from the miner.
    - miner_uids (List[int]): A list of miner UIDs. The miner at a particular index has a response in responses at the same index.
    """

    # common wandb setup
    prompt = task.synapse.prompt
    wandb_basics = {
        "task_name": task.name,
        "prompt": prompt,
        "validator_uid": validator.metagraph.hotkeys.index(validator.wallet.hotkey.ss58_address),
    }

    # track which miner uids are scored for updating the scores
    temp_miner_uids = []
    scores = []
    for i, response in enumerate(responses):
        miner_uid = miner_uids[i]
        reward = task.reward(validator, response)

        # means we got all of the information we need to score the miner and update wandb
        if len(reward) == 4:
            score, max_possible_score, task_results, correct_answer = reward
            # make sure the score is not None
            if score and max_possible_score:
                normalized_score = score/max_possible_score
                scores.append(normalized_score)
                temp_miner_uids.append(miner_uid)
                # extra transparent details for miners
                result = f"""
[bold]Task: {task.name}[/bold]\n[bold]Results:[/bold]
=====================\n"""+"\n".join(task_results) + f"""
[bold]Total reward:[/bold] {score}
[bold]Total possible reward:[/bold] {max_possible_score}
[bold]Normalized reward:[/bold] {normalized_score}
---
Stats with this validator:
Your Average Score: {validator.scores[miner_uid]}
Highest Score across all miners: {validator.scores.max()}
Median Score across all miners: {validator.scores.median()}"""

                # For generated/evaluated tasks, we send the results back to the miner so they know how they did and why
                # The dendrite client queries the network to send feedback to the miner
                _ = validator.dendrite.query(
                    # Send the query to selected miner axons in the network.
                    axons=[validator.metagraph.axons[miner_uid]],
                    # Construct a query. 
                    synapse=QnAResult(results=result),
                    # All responses have the deserialize function called on them before returning.
                    # You are encouraged to define your own deserialization function.
                    deserialize=False,
                    timeout=0.1 # quick b/c we are not awaiting a response
                )

                # log to wandb
                asyncio.create_task(log_to_wandb(response, validator, wandb_basics, task_results, miner_uid, score, max_possible_score, normalized_score, correct_answer))
                    
        elif len(reward) == 2: # skip it
            #bt.logging.debug(f"Skipping results for this task b/c Task API seems to have rebooted: {reward[1]}")
            time.sleep(25)
            continue
        else:
            #bt.logging.debug(f"Skipping results for this task b/c not enough information")
            time.sleep(25)
            continue

    # Update the scores based on the rewards. You may want to define your own update_scores function for custom behavior.
    miner_uids = torch.tensor(temp_miner_uids)
    validator.update_scores(torch.FloatTensor(scores).to(validator.device), miner_uids)