# 中秋文化市集保障

可复用的公共文化活动保障后台：把流程、容量、印章规则、人员、场地、
原料批次、设备检查与年龄限制编成一份目录，运行时后台据此提供
预约、扫码服务、断网补传与闭场对账。所有示例均为虚构数据，不含真实
个人信息、账号或访问凭据。

## 目录结构

- `contracts/context.schema.json` / `fixtures/context.json` — 领域上下文（原有最小资料）
- `contracts/market.schema.json` — 活动保障目录的对外契约
- `fixtures/market.json` — 中秋市集完整样例目录（六类项目、28 个场次、
  14 个原料批次、10 台设备、3 个样例家庭）
- `src/culture_market/catalog.py` — 目录载入与静态校验（引用完整、
  场地/人员时间不冲突、批次不超分、容量不超场地）
- `src/culture_market/ledger.py` — 批次物料台账（守恒恒等式）
- `src/culture_market/backend.py` — 运行时后台
- `src/culture_market/reports.py` — 工作人员看板与闭场对账/家庭还原
- `scripts/demo.py` — 端到端演示（`python -m scripts.demo`）
- `tests/test_market.py` — 32 个不变量测试

## 核心规则

- **幂等**：每个命令带 `request_id`；断网补传（`sync`）、重复扫码、
  重试都只执行一次，重复提交返回首次结果。
- **成功才消耗**：扫码服务先依次核验重复、预约/候补、年龄、同意项
  （过敏提示/影像授权/安全确认）、有效容量、物料预检，全部通过才扣
  名额与物料；任何拦截只记未服务台账并给出原因。
- **有效容量** = min(活动容量, 场地容量, 在用设备折算容量)。设备停用
  立即收紧；开场检查不合格的设备自动停用。
- **物料守恒**：入库 = 发放 + 报废 + 结余；发放/报废逐批摊分（FEFO），
  换场调剂走“退回中央池再分配”，不凭空产生物料。
- **家庭结伴，记录独立**：结伴预约整组同进同出；印章、阅读、科普
  记录均按成员独立保存。科普包每家庭限领一份，任务记录建到每位儿童。
- **可审计**：全部状态变化进哈希链日志，`verify_audit_chain()` 可离线
  校验；闭场后主办方得到逐批资源去向、逐案未服务原因，家庭可还原
  自己的真实参与过程。

## 本地校验与演示

```bash
python -m unittest discover -s tests   # 或 python -m pytest tests/
python -m scripts.demo                 # 一个市集下午的完整演示
```

## 复用到下一场活动

复制 `fixtures/market.json`，替换事件、场地、人员、活动配方、场次、
批次、设备与家庭名单，保持 `contracts/market.schema.json` 的结构；
目录载入时的静态校验会挡住排程冲突与超分，运行时不变量不变。
