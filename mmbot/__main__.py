"""python -m mmbot 与 run.command 使用同一个四阶段入口。"""
from .launcher import main

if __name__ == '__main__':
    raise SystemExit(main())
