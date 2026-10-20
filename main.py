"""程序入口：默认启动悬浮窗，--cli 启动命令行多轮对话。"""

import sys
import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="面试助手")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="使用命令行文字对话")
    mode.add_argument("--list-audio-devices", action="store_true", help="列出系统声音输出设备，不录音")
    args = parser.parse_args()
    if args.list_audio_devices:
        from system_audio import SystemAudioError, list_output_devices

        try:
            devices = list_output_devices()
        except SystemAudioError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not devices:
            print("没有可用的系统声音回环设备，请连接扬声器或耳机后重试。")
            return 1
        for device in devices:
            default = "（默认）" if device["is_default"] else ""
            print(f"{device['index']}: {device['name']} {default}")
        return 0
    if args.cli:
        from loop import main as run
    else:
        from floating_window import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
