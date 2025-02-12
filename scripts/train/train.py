# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0
import os
import sys
import warnings
from typing import Dict

import torch
from composer import Trainer
from composer.core import Evaluator
from composer.utils import dist, get_device, reproducibility
from omegaconf import DictConfig
from omegaconf import OmegaConf as om
from transformers import PreTrainedTokenizerBase

from llmfoundry import (COMPOSER_MODEL_REGISTRY, ComposerHFCausalLM,
                        MPTForCausalLM, build_finetuning_dataloader,
                        build_text_denoising_dataloader)
from llmfoundry.data.text_data import build_text_dataloader
from llmfoundry.utils.builders import (build_algorithm, build_callback,
                                       build_icl_evaluators, build_logger,
                                       build_optimizer, build_scheduler,
                                       build_tokenizer)
from llmfoundry.utils.config_utils import (log_config, process_init_device,
                                           update_batch_size_info)


def validate_config(cfg: DictConfig):
    """Validates compatible model and dataloader selection."""
    loaders = [cfg.train_loader]
    if 'eval_loader' in cfg:
        loaders.append(cfg.eval_loader)
    for loader in loaders:
        if loader.name == 'text':
            if cfg.model.name in ['hf_prefix_lm', 'hf_t5']:
                raise ValueError(
                    f'Model type "{cfg.model.name}" is not supported when using the "text " ' +\
                    f'dataloader. Please use the "text_denoising" dataloader to pre-train that model type.')
        elif loader.name == 'text_denoising':
            if cfg.model.name == 'hf_causal_lm':
                raise ValueError(
                    f'Model type "{cfg.model.name}" is not supported when using the "text_denoising" ' +\
                    f'dataloader. Please use the "text" dataloader to pre-train that model type.')
            if loader.mixture_of_denoisers.decoder_only_format and cfg.model.name == 'hf_t5':
                warnings.warn(
                    'Model type "hf_t5" requires `decoder_only_format` to be ``False``. ' +\
                    'Overriding `decoder_only_format` from ``True`` to ``False``.')
                loader.mixture_of_denoisers.decoder_only_format = False
            if (not loader.mixture_of_denoisers.decoder_only_format
               ) and cfg.model.name == 'hf_prefix_lm':
                warnings.warn(
                    'Model type "hf_prefix_lm" requires `decoder_only_format` to be ``True``. ' +\
                    'Overriding `decoder_only_format` from ``False`` to ``True``.')
                loader.mixture_of_denoisers.decoder_only_format = True

    if 'icl_tasks' in cfg:
        if cfg.model.name == 'hf_t5':
            raise ValueError(
                'ICL evaluation does not currently support Encoder-Decoder models, such as "hf_t5".'
            )

    if (cfg.model.get('fc_type', 'torch') != 'te' and 'te' not in cfg.model.get(
            'ffn_config', {}).get('ffn_type', 'mptmlp') and
            'fp8' in cfg.precision):
        warnings.warn(
            "fp8 only supported for te.Linear layers. Either set `cfg.model.fc_typ='te'` or "
            +
            "`cfg.model.ffn_config.ffn_type='te_ln_mlp'` to enable layers using fp8 precision."
        )

    if (cfg.model.get('fc_type', 'torch') == 'te' or
            'te' in cfg.model.get('ffn_config', {}).get('ffn_type', 'mptmlp')):
        fsdp_config = cfg.get('fsdp_config', None)
        act_ckpt = fsdp_config.get('activation_checkpointing', False)
        act_ckpt_reentrant = fsdp_config.get(
            'activation_checkpointing_reentrant', True)
        if fsdp_config is not None and act_ckpt == True and act_ckpt_reentrant == False:
            warnings.warn(
                '`te.Linear` layers do not support activation_checkpointing with '
                + '`activation_checkpointing_reentrant = False`. ' +
                'Setting cfg.fsdp_config.activation_checkpointing_reentrant=True.'
            )
            cfg.fsdp_config.activation_checkpointing_reentrant = True

    if 'te' in cfg.model.get('ffn_config', {}).get('ffn_type', 'mptmlp'):
        warnings.warn(
            '`te.LayerNormMLP` requires has issues with torch._dynamo. ' +
            'Setting `torch._dynamo.config.suppress_errors = True` and falling back to eager.'
        )
        torch._dynamo.config.suppress_errors = True  # type: ignore


def build_composer_model(model_cfg: DictConfig,
                         tokenizer: PreTrainedTokenizerBase):
    warnings.filterwarnings(
        action='ignore',
        message='Torchmetrics v0.9 introduced a new argument class property')
    if model_cfg.name not in COMPOSER_MODEL_REGISTRY:
        raise ValueError(
            f'Not sure how to build model with name={model_cfg.name}')
    return COMPOSER_MODEL_REGISTRY[model_cfg.name](model_cfg, tokenizer)


def build_composer_peft_model(
        model_cfg: DictConfig, lora_cfg: DictConfig,
        tokenizer: PreTrainedTokenizerBase) -> ComposerHFCausalLM:
    try:
        from peft import LoraConfig, get_peft_model
    except ImportError as e:
        raise ImportError(
            'Error importing from peft. Please verify that peft and peft utils '
            +
            'are installed by running `pip install -e .[peft]` from `llm-foundry/`. '
            + f'Error encountered: {e}')

    # 1) loads a hf model, 2) adds peft modules, 3) wraps it in a ComposerHFCausalLM.
    print('Building Lora config...')
    lora_cfg = LoraConfig(**lora_cfg.args)

    print('Building model from HuggingFace checkpoint...')
    model = MPTForCausalLM.from_pretrained(
        cfg.model.pretrained_model_name_or_path, trust_remote_code=True)
    print('Model built!')

    print('Adding Lora modules...')
    model = get_peft_model(model, lora_cfg)
    print('Lora modules added!')

    model = ComposerHFCausalLM(model, tokenizer)

    return model


def print_trainable_parameters(model: torch.nn.Module) -> None:
    # Prints the number of trainable parameters in the model.
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    print(
        f'trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}'
    )


def build_dataloader(cfg: DictConfig, tokenizer: PreTrainedTokenizerBase,
                     device_batch_size: int):
    if cfg.name == 'text':
        return build_text_dataloader(
            cfg,
            tokenizer,
            device_batch_size,
        )
    elif cfg.name == 'text_denoising':
        return build_text_denoising_dataloader(
            cfg,
            tokenizer,
            device_batch_size,
        )
    elif cfg.name == 'finetuning':
        return build_finetuning_dataloader(
            cfg,
            tokenizer,
            device_batch_size,
        )

    else:
        raise ValueError(f'Not sure how to build dataloader with config: {cfg}')


def main(cfg: DictConfig):
    # Check for incompatibilities between the model and data loaders
    validate_config(cfg)

    max_split_size_mb = cfg.get('max_split_size_mb', None)
    if max_split_size_mb is not None:
        os.environ[
            'PYTORCH_CUDA_ALLOC_CONF'] = f'max_split_size_mb:{max_split_size_mb}'

    # Filter deprecation warning from torch internal usage
    warnings.filterwarnings(
        action='ignore',
        category=UserWarning,
        message=
        f'torch.distributed.*_base is a private function and will be deprecated.*'
    )

    cfg.dist_timeout = cfg.get('dist_timeout', 600.0)

    reproducibility.seed_all(cfg.seed)
    dist.initialize_dist(get_device(None), timeout=cfg.dist_timeout)

    # Run Name
    if cfg.get('run_name') is None:
        cfg.run_name = os.environ.get('RUN_NAME', 'llm')

    # Get batch size info
    cfg = update_batch_size_info(cfg)

    # Read FSDP Config as a dict
    fsdp_config = cfg.get('fsdp_config', None)
    fsdp_config = om.to_container(fsdp_config,
                                  resolve=True) if fsdp_config else None
    assert isinstance(fsdp_config, Dict) or fsdp_config is None
    if dist.get_world_size() == 1 and fsdp_config is not None:
        warnings.warn(
            'FSDP is not applicable for single-GPU training. Reverting to DDP.')
        cfg.pop('fsdp_config')
        fsdp_config = None

    init_context = process_init_device(cfg.model, fsdp_config)

    # build tokenizer
    tokenizer = build_tokenizer(cfg.tokenizer)

    # Build Model
    print('Initializing model...')
    model = build_composer_model(cfg.model, tokenizer)
    cfg.n_params = sum(p.numel() for p in model.parameters())
    print(f'{cfg.n_params=:.2e}')

    # Dataloaders
    print('Building train loader...')
    train_loader = build_dataloader(
        cfg.train_loader,
        tokenizer,
        cfg.device_train_batch_size,
    )
    print('Building eval loader...')
    evaluators = []
    if 'eval_loader' in cfg:
        assert model.train_metrics is not None
        eval_loader = Evaluator(label='eval',
                                dataloader=build_dataloader(
                                    cfg.eval_loader, tokenizer,
                                    cfg.device_eval_batch_size),
                                metric_names=list(model.train_metrics.keys()))
        evaluators.append(eval_loader)

    if 'icl_tasks' in cfg:
        icl_evaluators, _ = build_icl_evaluators(cfg.icl_tasks, tokenizer,
                                                 cfg.max_seq_len,
                                                 cfg.device_eval_batch_size)
        evaluators.extend(icl_evaluators)

    # Optimizer
    optimizer = build_optimizer(cfg.optimizer, model)

    # Scheduler
    scheduler = build_scheduler(cfg.scheduler)

    # Loggers
    loggers = [
        build_logger(name, logger_cfg)
        for name, logger_cfg in (cfg.get('loggers') or {}).items()
    ]

    # Callbacks
    callbacks = [
        build_callback(name, callback_cfg)
        for name, callback_cfg in (cfg.get('callbacks') or {}).items()
    ]

    # Algorithms
    algorithms = [
        build_algorithm(name, algorithm_cfg)
        for name, algorithm_cfg in (cfg.get('algorithms') or {}).items()
    ]

    # Set autoresume default on if possible
    save_folder = cfg.get('save_folder', None)
    save_latest_filename = cfg.get('save_latest_filename',
                                   'latest-rank{rank}.pt')
    save_overwrite = cfg.get('save_overwrite', False)
    save_weights_only = cfg.get('save_weights_only', False)
    autoresume_default = False
    if cfg.run_name is not None and save_folder is not None and save_latest_filename is not None and not save_overwrite and not save_weights_only:
        print(
            'As run_name, save_folder, and save_latest_filename are set, changing autoresume default to True...'
        )
        autoresume_default = True

    # Build the Trainer
    print('Building trainer...')
    trainer = Trainer(
        run_name=cfg.run_name,
        seed=cfg.seed,
        model=model,
        train_dataloader=train_loader,
        eval_dataloader=evaluators,
        optimizers=optimizer,
        schedulers=scheduler,
        max_duration=cfg.max_duration,
        eval_interval=cfg.eval_interval,
        eval_subset_num_batches=cfg.get('eval_subset_num_batches', -1),
        progress_bar=cfg.get('progress_bar', False),
        log_to_console=cfg.get('log_to_console', True),
        console_log_interval=cfg.get('console_log_interval', '1ba'),
        loggers=loggers,
        callbacks=callbacks,
        precision=cfg.precision,
        algorithms=algorithms,
        device_train_microbatch_size=cfg.get('device_train_microbatch_size',
                                             'auto'),
        fsdp_config=fsdp_config,  # type: ignore
        save_folder=save_folder,
        save_filename=cfg.get('save_filename',
                              'ep{epoch}-ba{batch}-rank{rank}.pt'),
        save_latest_filename=save_latest_filename,
        save_interval=cfg.get('save_interval', '1000ba'),
        save_num_checkpoints_to_keep=cfg.get('save_num_checkpoints_to_keep',
                                             -1),
        save_overwrite=save_overwrite,
        save_weights_only=save_weights_only,
        load_path=cfg.get('load_path', None),
        load_weights_only=cfg.get('load_weights_only', False),
        load_ignore_keys=cfg.get('load_ignore_keys', None),
        autoresume=cfg.get('autoresume', autoresume_default),
        python_log_level=cfg.get('python_log_level', 'debug'),
        dist_timeout=cfg.dist_timeout,
    )

    torch.cuda.empty_cache()

    print('Logging config...')
    log_config(cfg)

    if cfg.get('eval_first',
               False) and trainer.state.timestamp.batch.value == 0:
        trainer.eval()

    print('Starting training...')
    trainer.fit()

    print('Done.')


if __name__ == '__main__':
    yaml_path, args_list = sys.argv[1], sys.argv[2:]
    with open(yaml_path) as f:
        yaml_cfg = om.load(f)
    cli_cfg = om.from_cli(args_list)
    cfg = om.merge(yaml_cfg, cli_cfg)
    assert isinstance(cfg, DictConfig)
    main(cfg)
