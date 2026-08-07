import os
import argparse
import subprocess
import sys
from config_bootstrap import ensure_config_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def start_profile(profile_name, config_name, once=False):
    print(f"\n{'='*50}\n正在运行分身: {profile_name if profile_name else '默认(default)'}\n{'='*50}")
    env = os.environ.copy()
    if profile_name:
        env['JOB_PROFILE'] = profile_name
    else:
        if 'JOB_PROFILE' in env:
            del env['JOB_PROFILE']
            
    command = [sys.executable, "main.py", "--config", config_name]
    if once:
        command.append("--once")
    return subprocess.Popen(
        command,
        env=env,
        cwd=BASE_DIR,
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="运行所有用户分身")
    parser.add_argument('--once', action='store_true', help='每个分身只检查一次后退出')
    args = parser.parse_args()
    ensure_config_file('config.yaml')

    # 只运行默认配置和 config_user*.yaml；公开示例模板不会被执行。
    configs = ['config.yaml']
    configs.extend(
        name for name in os.listdir(BASE_DIR)
        if name.startswith('config_user') and name.endswith('.yaml')
    )
    configs = [name for name in configs if os.path.exists(os.path.join(BASE_DIR, name))]
    
    if not configs:
        print("没有找到任何配置文件(config*.yaml)")
        sys.exit(1)
        
    processes = []
    try:
        for config_name in sorted(set(configs)):
            # config.yaml -> ''；config_user2.yaml -> 'user2'
            profile = config_name[len('config'):-len('.yaml')].lstrip('_')
            processes.append(start_profile(profile, config_name, once=args.once))

        exit_codes = [process.wait() for process in processes]
        if any(code != 0 for code in exit_codes):
            failed_count = sum(code != 0 for code in exit_codes)
            print(f"\n{failed_count} 个配置运行失败。")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n正在停止所有监测分身...")
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
