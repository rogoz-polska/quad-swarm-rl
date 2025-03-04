from sample_factory.launcher.run_description import Experiment, ParamGrid, RunDescription
from swarm_rl.runs.quad_multi_mix_baseline import QUAD_BASELINE_CLI

_params = ParamGrid(
    [
        ("seed", [0000, 1111, 2222, 3333]),
    ]
)

SMALL_MODEL_CLI = QUAD_BASELINE_CLI + (
    " --train_for_env_steps=10000000000 --num_workers=36 --num_envs_per_worker=4 "
    "--quads_num_agents=8 --save_milestones_sec=10000 --async_rl=True --num_batches_to_accumulate=8 "
    "--serial_mode=False --batched_sampling=True --normalize_input=True --normalize_returns=True "
    "--with_wandb=True --wandb_tags multi obstacles "
    "--quads_obstacle_mode=dynamic --quads_obstacle_num=1 --quads_obstacle_type=sphere --quads_obstacle_traj=mix "
    "--quads_collision_obstacle_reward=5.0 --quads_obstacle_obs_mode=absolute --quads_collision_obst_smooth_max_penalty=10.0 "
    "--quads_obstacle_hidden_size=256 --replay_buffer_sample_prob=0.0 --quads_obst_penalty_fall_off=10.0"
)

_experiment = Experiment(
    "baseline_multi_drone_obstacle",
    SMALL_MODEL_CLI,
    _params.generate_params(randomize=False),
)

RUN_DESCRIPTION = RunDescription("quad_multi_baseline_obstacle", experiments=[_experiment])
# python -m sample_factory.launcher.run --run=swarm_rl.runs.sf2_multi_drone_obstacle --runner=processes --max_parallel=1 --pause_between=1 --experiments_per_gpu=4 --num_gpus=1
