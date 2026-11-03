# ETF Options Market Maker

基于 Level 1 行情的 ETF 期权做市研究项目，涵盖期权定价、事件驱动报价、组合压力风控、订单执行与成交归因。系统将未成交挂单视为潜在敞口，在下单前联合检查持仓与订单的压力损失。

## 策略设计

- **定价**：使用 Black–Scholes 模型计算公允价和 Greeks，从期权报价反解隐含波动率，以 ETF 标的行情驱动公允价。
- **报价**：依据公允价和市场报价生成限价单，按价格变化触发改价，等待撤单确认后补单。
- **风控**：对标的价格和波动率冲击进行全额重定价，同时约束空头张数、Delta、Gamma、Vega 和资金占用。
- **执行**：使用幂等键跟踪未知订单，按原委托对账和有限重试；断线或触发限额时停止新增风险。
- **归因**：结合手续费、成交价和一分钟后的公允价，计算价差、返佣、markout 与净 Maker Edge。退出成本为估计值，不等同于实际利润。
- **覆盖**：在单合约上积累成交证据，再依据实测 Edge、压力预算和切换成本决定多合约覆盖。

## 运行流程

1. **只读采集**：订阅行情，核对账户限额和合约数据。
2. **撮合探针**：小额验证成交角色、费用和系统估值。
3. **单合约报价**：每侧 1 张，记录完整时段的报价与成交表现。
4. **多合约覆盖**：取得时段记录和实测正 Edge 后扩大覆盖。

系统不会自动升级阶段。只读模式不下单；探针和做市阶段会向配置的账户发送委托。

## 快速开始

项目使用 Python 3.9 及以上版本，以及 `requests`、`websocket-client`。交易接口依赖学生端提供的 **simtrade SDK 3.0.1**；请使用获授权的 SDK 源码或安装包，不要从公共源安装同名包。

1. 将授权的 SDK 源码放在项目根目录的 `simtrade-sdk-3.0.1` 文件夹，运行 `./run.command --init` 创建配置，再在 `competition.json` 中填写交易站点地址和具体期货行情合约。期权合约可在只读采集后从 `data/universe.json` 核对。
2. 在 macOS 终端运行 `./run.command`。
3. 输入 API Key 并选择阶段 `1`，先运行只读采集。根据 competition.json 的配置项填写探针、定价、风险和策略参数。

`run.command` 默认使用本机 Miniforge 的 `option` 环境；其他机器可设置 `MMBOT_PYTHON` 指向已有虚拟环境中的 Python。API Key 只从环境变量或交互输入读取，不写入配置文件。

也可以直接指定阶段：

```bash
./run.command --status
./run.command --check --stage 1
./run.command --stage 1
./run.command --stage 2 --execute
./run.command --stage 3 --execute
./run.command --stage 4 --execute
```

后三条命令会实际下单。正式做市需要探针证据和已核实的规则信息；多合约覆盖还需要单合约时段记录与实测收益准入。

## 项目结构

```text
mmbot/
├── pricing.py             # BS 定价、IV 与 Greeks
├── risk.py                # 情景重定价与独立硬约束
├── quoter.py              # 报价生成与订单差分
├── trading_execution.py   # 发单前检查与执行
├── order_recovery.py      # 未知订单对账与恢复
├── edge.py                # 成交归因与 Edge 统计
├── coverage.py            # 覆盖切换与预算调整
├── probe.py               # 撮合与费用探针
└── launcher.py            # 统一启动入口
tests/                     # 单元测试与 SDK 假传输测试
docs/                      # 运行与模型说明
```

## 测试

在项目根目录运行离线测试，将 Python 路径替换为本机虚拟环境路径：

```bash
PYTHONPATH=simtrade-sdk-3.0.1 /opt/homebrew/Caskroom/miniforge/base/envs/option/bin/python -m unittest discover -s tests -v
```

测试使用模拟传输验证订单处理，不连接真实账户。

## 适用范围

当前实现面向指定模拟交易系统和四条 ETF 期权链；期货只用于行情订阅，不发送期货委托。压力情景、报价参数和费用估计依赖输入数据与配置，不能保证覆盖所有市场变化。

本项目用于策略研究与模拟交易。真实撮合规则、费用、连接稳定性和完整交易时段表现需在目标环境中核实。
