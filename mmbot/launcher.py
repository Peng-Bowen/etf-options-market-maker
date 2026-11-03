"""四阶段统一入口：一份配置、同一数据目录、显式选择交易阶段。"""
import argparse
import copy
import fcntl
import getpass
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 优先使用学生端下载的官方源码，不从公共源安装同名包。
SDK = ROOT/'simtrade-sdk-3.0.1'
if str(SDK) not in sys.path:
    sys.path.insert(0,str(SDK))

LABELS = {1:'只读采集',2:'开盘探针（会下单）',3:'单合约做市（会下单）',4:'多合约做市（会下单）'}


def template():
    return dict(
        connection=dict(gateway_url='',futures=[]), data_dir='data',
        runtime=dict(duration_seconds=None),
        probe=dict(trade_date=None,premium_budget=500,attempts=[],observations=[]),
        pricing=dict(rate=.02,dividend=0.,iv_update_ms=60000,max_book_age_ms=5000,
                     max_iv_age_ms=300000,max_pair_skew_ms=3000),
        risk=dict(max_loss=15000,max_new_order_loss=10000,max_short_lots=5,
                  max_cash_delta_per_underlying=240000,max_cash_gamma=1000000,max_vega=100000,
                  available_cash=100000),
        greek_comparison=dict(atol=.001,rtol=.05),
        strategy=dict(calibration_symbol='',symbols=[],
            confirmations=dict(valuation='',quote_rule='',recovery_test='',risk_free_rate=''),
            quote=dict(half_spread_ticks=1,reprice_ticks=2,min_rest_ms=3000),
            coverage=dict(max_symbols=3,min_hold_ms=300000,switch_ratio=1.2,min_scale=.25,
                          scale_step=.25,adjust_ms=60000,relax_below=.5,tighten_above=.8),
            edge=dict(fair_tolerance_ms=5000,min_lots=20,min_blocks=5,evidence_max_age_ms=172800000)))


@dataclass(frozen=True)
class Project:
    path: Path
    raw: dict

    @property
    def data_dir(self):
        path = Path(self.raw.get('data_dir','data')).expanduser()
        return (path if path.is_absolute() else self.path.parent/path).resolve()


@dataclass(frozen=True)
class Prepared:
    stage: int
    config: object
    duration: float
    probe_plan: object = None
    risk_config: object = None
    strategy_settings: object = None


def load_project(path):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f'找不到统一配置 {path}；先运行 ./run.command --init')
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw,dict) or not isinstance(raw.get('connection'),dict):
        raise ValueError('配置须包含 connection 对象')
    forbidden = {'api_key','token','password','TRADING_API_KEY'}
    if forbidden.intersection(raw) or forbidden.intersection(raw['connection']):
        raise ValueError('API Key/口令只允许放在环境变量，不放进统一配置')
    return Project(path,raw)


def prepare(project, stage, env=None, duration=None):
    """纯本地解析，不访问账户、不创建临时分阶段配置。"""
    from .config import Config
    if stage not in LABELS:
        raise ValueError('阶段只能是 1、2、3、4')
    env = dict(os.environ if env is None else env)
    raw = project.raw
    connection = raw['connection']
    if connection.get('gateway_url'):
        env['TRADING_GATEWAY_URL'] = connection['gateway_url']
    if connection.get('futures'):
        if not isinstance(connection['futures'],list) or not all(isinstance(s,str) for s in connection['futures']):
            raise ValueError('connection.futures 必须为具体合约代码列表')
        env['TRADING_FUTURES'] = ','.join(connection['futures'])
    if env.get('TRADING_DATA_DIR') and Path(env['TRADING_DATA_DIR']).expanduser().resolve() != project.data_dir:
        raise ValueError('TRADING_DATA_DIR 与统一配置不一致，请取消该环境变量或统一路径，避免阶段状态断开')
    env['TRADING_DATA_DIR'] = str(project.data_dir)
    config = Config.from_env(env)
    seconds = duration if duration is not None else raw.get('runtime',{}).get('duration_seconds')
    if seconds is None:
        seconds = 1800 if stage == 1 else 0
    if isinstance(seconds,bool) or not isinstance(seconds,(float,int)) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('运行时长必须是非负有限秒数，0 表示持续运行')
    plan = risk = settings = None
    try:
        if stage == 2:
            from .probe import parse_plan
            plan = parse_plan(raw['probe'])
            if not plan['attempts'] and not plan['observations']:
                raise ValueError('阶段二至少需要一项探针或 P6 观察计划')
            if plan['attempts'] and config.day_start_net is None:
                raise ValueError('交易探针需要设置 TRADING_DAY_START_NET')
        if stage in (3,4):
            from .strategy import parse_strategy_config
            from .valuation_monitor import parse_risk_config
            risk = parse_risk_config(dict(pricing=raw['pricing'],limits=raw['risk'],greek_comparison=raw['greek_comparison']))
            strategy = copy.deepcopy(raw['strategy'])
            calibration = strategy.pop('calibration_symbol',None)
            if stage == 3:
                if not isinstance(calibration,str) or not calibration:
                    raise ValueError('阶段三需要填写 strategy.calibration_symbol')
                strategy['symbols'] = [calibration]
                strategy['coverage']['max_symbols'] = 1
            strategy['stage'] = stage
            settings = parse_strategy_config(strategy,project.path.parent)
            for key in ('valuation','quote_rule','recovery_test','risk_free_rate'):
                if not isinstance(settings.confirmations.get(key),str) or not settings.confirmations[key].strip():
                    raise ValueError(f'需要填写真实核实依据：strategy.confirmations.{key}')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError(f'阶段 {stage} 配置不完整或无效：{exc}') from exc
    return Prepared(stage,config,float(seconds),plan,risk,settings)


def status(project):
    """只展示本地事实，不将程序退出解释为验收通过。"""
    root = project.data_dir
    result = dict(config=str(project.path),data_dir=str(root),data_exists=root.exists(),accounts=[])
    if not root.exists():
        return result
    workflow = root/'workflow.json'
    if workflow.exists():
        result['last_run'] = json.loads(workflow.read_text(encoding='utf-8')).get('last_run')
    universe = root/'universe.json'
    if universe.exists():
        result['option_count'] = len(json.loads(universe.read_text(encoding='utf-8')).get('options',[]))
    for prefix in ('probe-state-','strategy-state-'):
        for path in sorted(root.glob(prefix+'*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            unresolved = sum(a.get('status') not in ('filled','cancelled','rejected','conflict','not_found') for a in data.get('attempts',{}).values())
            result['accounts'].append(dict(file=path.name,account_id=data.get('account_id'),
                attempts=len(data.get('attempts',{})),trades=len(data.get('trades',{})),unresolved=unresolved,
                recorded_passed_sessions=sum(bool(s.get('passed')) for s in data.get('stage3_sessions',[]))))
    result['notice'] = '本地记录不替代实际账户核对；未自动升级阶段。'
    return result


def _write_workflow(path, record):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(last_run=record),ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def launch(project, stage, env=None, *, execute=False, duration=None):
    if stage != 1 and not execute:
        raise ValueError('阶段 2/3/4 会实际下单，请明确添加 --execute，或在菜单中选择该交易阶段')
    prepared = prepare(project,stage,env,duration)
    from .journal import SecretFilter, redact
    from .main import run
    root = prepared.config.data_dir
    root.mkdir(parents=True,exist_ok=True)
    with (root/'launcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('这个数据目录已有阶段在运行，请先停止原进程') from exc
        record = dict(stage=stage,label=LABELS[stage],status='running',pid=os.getpid(),
                      started_at=datetime.now(timezone.utc).isoformat(),config=str(project.path))
        path = root/'workflow.json'
        _write_workflow(path,record)
        for handler in logging.getLogger().handlers:
            handler.addFilter(SecretFilter(prepared.config.api_key))
        print(f'启动阶段 {stage}：{LABELS[stage]}\n数据目录：{root}',flush=True)
        try:
            result = run(prepared.config,prepared.duration,probe_plan=prepared.probe_plan,
                         risk_config=prepared.risk_config,strategy_settings=prepared.strategy_settings)
            record['status'] = 'exited'
            return result
        except BaseException as exc:
            record['status'] = 'interrupted' if isinstance(exc,KeyboardInterrupt) else 'error'
            record['error'] = redact(str(exc),prepared.config.api_key)
            raise
        finally:
            record['ended_at'] = datetime.now(timezone.utc).isoformat()
            record['notice'] = '退出不表示阶段验收通过，也不表示账户已经清仓。'
            _write_workflow(path,record)


def _menu():
    print('\nETF 期权策略 · 四阶段统一入口')
    for stage,label in LABELS.items():
        print(f'  {stage}. {label}')
    print('  5. 检查配置（不联网）\n  6. 查看进度（不联网）\n  0. 退出')
    answer = input('请选择 [默认 1]：').strip() or '1'
    if answer in ('0','6'):
        return answer,None
    if answer == '5':
        stage = input('检查哪个阶段 [1–4，默认 1]：').strip() or '1'
        return 'check',int(stage)
    return 'run',int(answer)


def main(argv=None):
    parser = argparse.ArgumentParser(description='统一运行四个阶段；默认菜单，不自动升级到交易阶段。')
    parser.add_argument('--config',type=Path,default=ROOT/'competition.json',help='统一配置路径')
    parser.add_argument('--stage',type=int,choices=(1,2,3,4),help='运行或检查指定阶段')
    parser.add_argument('--execute',action='store_true',help='明确授权阶段 2/3/4 实际下单')
    parser.add_argument('--duration',type=float,help='覆盖运行秒数；0 持续运行')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--init',action='store_true',help='创建统一配置模板，不覆盖已有配置')
    actions.add_argument('--status',action='store_true',help='只查看本地阶段记录')
    actions.add_argument('--check',action='store_true',help='只检查指定阶段本地配置，默认阶段 1')
    args = parser.parse_args(argv)
    runtime_env = dict(os.environ)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.init:
            args.config.parent.mkdir(parents=True,exist_ok=True)
            with args.config.open('x',encoding='utf-8') as stream:
                json.dump(template(),stream,ensure_ascii=False,indent=2)
            print(f'已创建配置：{args.config}\n请填写学生站、具体期货合约及各阶段所需信息；模板参数未经实测校准。')
            return 0
        project = load_project(args.config)
        action = 'status' if args.status else 'check' if args.check else 'run'
        stage = args.stage or 1
        execute = args.execute
        if args.stage is None and not (args.status or args.check):
            if not sys.stdin.isatty():
                raise ValueError('非交互运行请指定 --stage 1/2/3/4，或使用 --status / --check')
            action,stage = _menu()
            if action == '0':
                return 0
            if action == '6':
                action = 'status'
            execute = action == 'run' and stage in (2,3,4)
        if action == 'status':
            print(json.dumps(status(project),ensure_ascii=False,indent=2))
        elif action == 'check':
            check_env = dict(runtime_env)
            missing_key = not check_env.get('TRADING_API_KEY')
            if missing_key:
                check_env['TRADING_API_KEY'] = 'ak_LOCAL_CONFIG_CHECK_NOT_A_REAL_KEY'
            prepared = prepare(project,stage,check_env,duration=args.duration)
            print(f'阶段 {stage} 本地配置通过。数据目录：{prepared.config.data_dir}')
            print('没有联网、没有下单；真实账户、探针记录及正 Edge 准入仍在运行时核对。')
            if missing_key:
                print('尚未提供 API Key；正式运行时请设置环境变量或在菜单中输入。')
        else:
            if stage != 1 and not execute:
                raise ValueError('阶段 2/3/4 会实际下单，请添加 --execute，或在菜单中选择该阶段')
            if sys.stdin.isatty():
                # 先检查公共连接配置，避免未填服务器地址时就索取密钥。
                common = dict(runtime_env,TRADING_API_KEY='ak_LOCAL_CONFIG_CHECK_NOT_A_REAL_KEY')
                prepare(project,1,common,duration=args.duration)
                if not runtime_env.get('TRADING_API_KEY'):
                    runtime_env['TRADING_API_KEY'] = getpass.getpass('API Key（输入不显示，仅本次进程使用，不保存）：').strip()
                needs_baseline = stage in (3,4) or (stage == 2 and project.raw.get('probe',{}).get('attempts'))
                if needs_baseline and not runtime_env.get('TRADING_DAY_START_NET'):
                    value = input('当日期初净值（须已核实；阶段三/四当日已保存时可留空）：').strip()
                    if value:
                        runtime_env['TRADING_DAY_START_NET'] = value
            launch(project,stage,runtime_env,execute=execute,duration=args.duration)
        return 0
    except KeyboardInterrupt:
        print('已中断。请以状态文件和账户终态确认是否仍有委托。',file=sys.stderr)
        return 130
    except (ValueError,RuntimeError,OSError,KeyError,TypeError) as exc:
        from .journal import redact
        print(redact(str(exc),runtime_env.get('TRADING_API_KEY','')),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
