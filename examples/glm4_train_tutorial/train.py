# Copyright 2024 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""glm make experience and train"""

import os
import argparse
import mindspore as ms
import mindspore.communication.management as D
from mindspore import context, ops
from mindspore.communication.management import get_rank

from mindformers import MindFormerConfig, logger
from mindformers.models.glm2 import ChatGLM2Config
from mindformers.core.context import build_context
from mindformers.core.parallel_config import build_parallel_config
from mindformers.tools.utils import str2bool
from mindrlhf.utils.configs import combine_config, init_ppo_dataset, init_network_and_optimizer
from mindrlhf.trainer.ppo_trainer import PPOTrainer
from mindrlhf.configs.ppo_configs import PPOConfig


def make_experience(sft_model_config, ppo_config, ref_model_config, critic_model_config, rm_model_config,
                    enable_compile_cache):
    context.set_context(enable_compile_cache=enable_compile_cache, compile_cache_path='./generate_cache')

    ppo_config.save_ckpt_dir = None

    trainer = PPOTrainer(ppo_config=ppo_config, sft_model_config=sft_model_config, ref_model_config=ref_model_config,
                         rm_model_config=rm_model_config, critic_model_config=critic_model_config)
    trainer.pre_run("generate")
    # ================= pre compile model for loading dist ckpt ========================
    batch_size = ppo_config.batch_size * ppo_config.parallel_config.get("data_parallel", 1)
    fake_data = ops.zeros((batch_size, ppo_config.seq_length), ms.int32)
    logger.info("Start pre compile model")
    trainer.ppo_model.policy_model.add_flags_recursive(use_past=False)
    trainer.ppo_model.policy_model.add_flags_recursive(is_first_iteration=True)
    trainer.ppo_model.policy_model.compile(fake_data, samples=fake_data, return_value=True)
    trainer.ref_model.compile(fake_data, samples=fake_data)
    trainer.reward_fn.compile(fake_data)
    # ====================================================================================
    trainer.load_checkpoint()

    trainer.make_experience(num_rollouts=ppo_config.num_rollouts, rank_id=get_rank())
    logger.info("End of making experience")


def train(sft_model_config, ppo_config, ref_model_config, critic_model_config, rm_model_config, enable_compile_cache):
    context.set_context(enable_compile_cache=enable_compile_cache, compile_cache_path='./training_cache')

    sft_model_config.model.model_config.use_past = False
    ppo_config.mind_dataset_dir = None

    ref_model_config.checkpoint_name_or_path = None
    rm_model_config.checkpoint_name_or_path = None

    trainer = PPOTrainer(ppo_config=ppo_config, sft_model_config=sft_model_config, ref_model_config=ref_model_config,
                         rm_model_config=rm_model_config, critic_model_config=critic_model_config)
    trainer.pre_run("train")
    dataset = init_ppo_dataset(trainer)
    data = next(dataset.create_tuple_iterator())
    ppo_with_grad = init_network_and_optimizer(trainer)
    ppo_with_grad.set_train(True)
    # =============== pre compile ppo with grad model ===========
    ppo_with_grad.compile(*data)
    # ===========================================================
    trainer.load_checkpoint()
    trainer.train(ppo_with_grad, dataset)
    logger.info("End of training")


def main(sft_path, reward_path, critic_path, use_parallel, enable_compile_cache, only_save_strategy,
         load_sft_checkpoint, mind_dataset_dir, save_data_file, save_ckpt_dir, load_ref_checkpoint, load_rm_checkpoint,
         load_critic_checkpoint):
    sft_config = MindFormerConfig(sft_path)
    sft_config.use_parallel = use_parallel
    os.environ["RUN_MODE"] = sft_config.run_mode

    # init context
    build_context(sft_config)
    build_parallel_config(sft_config)
    sft_config.model.model_config.parallel_config = sft_config.parallel_config

    if load_sft_checkpoint is not None:
        sft_config.load_checkpoint = load_sft_checkpoint
    sft_model_config = ChatGLM2Config(**sft_config.model.model_config)
    sft_model_config.checkpoint_name_or_path = load_sft_checkpoint
    sft_model_config.model_name = "glm4"

    # init ppo config
    ppo_config = PPOConfig()
    ppo_config.mind_dataset_dir = mind_dataset_dir
    ppo_config.save_ckpt_dir = save_ckpt_dir
    ppo_config.save_data_file = save_data_file
    ppo_config.only_save_strategy = only_save_strategy
    ppo_config.align_type = "rlhf_stages"
    ppo_config.use_parallel = use_parallel
    ppo_config = combine_config(ppo_config, sft_model_config)

    # init ref model
    ref_config = MindFormerConfig(sft_path)
    ref_config.use_parallel = use_parallel
    ref_config.model.model_config.parallel_config = ref_config.parallel_config
    ref_config.model.model_config.use_past = False
    if load_ref_checkpoint is not None:
        ref_config.load_checkpoint = load_ref_checkpoint
    ref_model_config = ChatGLM2Config(**ref_config.model.model_config)
    ref_model_config.checkpoint_name_or_path = load_ref_checkpoint
    ref_model_config.model_name = "glm4"

    # init reward model
    rm_config = MindFormerConfig(reward_path)
    rm_config.use_parallel = use_parallel
    rm_config.model.model_config.parallel_config = rm_config.parallel_config
    rm_config.model.model_config.use_past = False

    rm_model_config = ChatGLM2Config(**rm_config.model.model_config)
    if load_rm_checkpoint is not None:
        rm_model_config.checkpoint_name_or_path = load_rm_checkpoint
    rm_model_config.model_name = "glm4"

    # init critic model
    critic_config = MindFormerConfig(critic_path)
    critic_config.use_parallel = use_parallel
    critic_config.model.model_config.parallel_config = critic_config.parallel_config
    critic_config.model.model_config.use_past = False
    if load_critic_checkpoint is not None:
        critic_config.load_checkpoint = load_critic_checkpoint
    critic_model_config = ChatGLM2Config(**critic_config.model.model_config)
    critic_model_config.checkpoint_name_or_path = load_critic_checkpoint
    critic_model_config.model_name = "glm4"

    make_experience(sft_model_config, ppo_config, ref_model_config, critic_model_config, rm_model_config,
                    enable_compile_cache)
    train(sft_model_config, ppo_config, ref_model_config, critic_model_config, rm_model_config, enable_compile_cache)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='glm4 make experience')
    parser.add_argument('--sft_path', type=str, default=None, help='sft model path', required=True)
    parser.add_argument('--reward_path', type=str, default=None, help='reward model path', required=True)
    parser.add_argument('--critic_path', type=str, default=None, help='critic model path', required=True)
    parser.add_argument('--save_data_file', type=str, default=None, help='save_data_file', required=True)
    parser.add_argument('--save_ckpt_dir', type=str, default="./", help='save_ckpt_dir')
    parser.add_argument('--mind_dataset_dir', type=str, default=None, help='mind_dataset_dir', required=True)
    parser.add_argument('--use_parallel', type=str2bool, default=False, help='use_parallel')
    parser.add_argument('--load_sft_checkpoint', type=str, default=None, help='load checkpoint path')
    parser.add_argument('--load_rm_checkpoint', type=str, default=None, help='load checkpoint path')
    parser.add_argument('--load_critic_checkpoint', type=str, default=None, help='load checkpoint path')
    parser.add_argument('--load_ref_checkpoint', type=str, default=None, help='load checkpoint path')
    parser.add_argument('--enable_compile_cache', type=str2bool, default=False, help='enable compile cache')
    parser.add_argument('--only_save_strategy', type=str2bool, default=False, help='only save strategy')
    args = parser.parse_args()
    main(args.sft_path, args.reward_path, args.critic_path, args.use_parallel, args.enable_compile_cache,
         args.only_save_strategy, args.load_sft_checkpoint, args.mind_dataset_dir, args.save_data_file,
         args.save_ckpt_dir, args.load_ref_checkpoint, args.load_rm_checkpoint, args.load_critic_checkpoint)