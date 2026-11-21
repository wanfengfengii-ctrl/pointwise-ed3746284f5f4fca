# 砺川材料试验室应变记录站

白班试验员向两台记录器提交带出处的有符号微应变原码。采集员逐件交原，质检员按正式首执安排复检，夜班用报告号核回当班证据。

## 运行

需要 Python 3.11 标准库。从项目根目录运行。

```sh
python3 -B check_project.py
python3 -B station_demo.py demo
python3 -B station_demo.py init ./workbench
```

`check_project.py` 是控制台提供的完整回归入口。也可直接运行 `python3 -B -m unittest discover -s tests -t .`。`demo` 在临时目录运行完整日剧本并输出实测判级和断源读回的班报。`init` 建立站库和两份独立源库，重复执行保留已存在的班次、原件、首执和报告。两条 CLI 命令发生运行错误时以非零码退出。样例只有在显式运行日剧本时才被加载。

## 一天的工作

1. 操作员冻结班次、左右设备顺序、通道和门槛 8 μɛ。
2. 试验台各提交一件波形，设备独立分配源序 1。
3. 采集员预览左台 L1；左台追加 L2 至 L4，L1 仍为队首。
4. 采集员实收 L1 至 L3 和 R1。每件先在站库一笔提交原件与首执，再由源库释放确切队首。
5. 质检员核对 L1=[-8,8,3] 的峰值 8 和首峰下标 0。L2=[-9,3] 超限，L3=[0,0] 是正式实收零峰。
6. 两台分别停采，末序为 4 和 1。质检员生成班报，左台缺口为闭区间 [4,4]。
7. 夜班断开两份源库，重开站库按报号领取判级、原波形、出处及首执。

## Python 接口

- `intake.ledger.StationLedger(path)` 和 `recorder.journal.DeviceJournal(path, device_id)` 各自通过 `initialize()` 建表。一个工作台使用一个站库及每台设备各自的一份源库。
- `shift.opening.open_shift(ledger, shift_id, devices, channel, threshold)` 冻结两台设备、有序清册及规程。同号同参复领，异参拒绝。
- `recorder.capture.capture(ledger, journal, shift_id, action_id, samples, source)` 返回带 `wave_id` 的原包。`wave_id` 由班号、设备号、源序组成。样点为非空 Python 整数序列，门槛为非负整数，布尔值非法。动作按班次及设备具名，原样重投复领，样点或出处冲突拒绝。
- `recorder.queue.peek(ledger, journal, shift_id)` 返回队首或 `None`。预览期间可继续追加。
- `intake.accept.take_one(ledger, journal, TakeOneRequest(request_id, shift_id, device_id, wave_id))` 办理普通实收。取件号在站库具名，首次正式实收后固定绑定。队首变化、错绑和另一请求已正式实收时拒绝。提交前失败保留源队首。提交后的失败可用原参数续办，同请求复领首执，迟到释放保留后继件。
- `quality.grading.grade(ledger, receipt_id)` 返回峰值、零基首次峰点、冻结规程与判级。峰值为最大绝对微应变，严格大于门槛才复检。未知首执或原件缺失报错。
- `recorder.endmark.stop(ledger, journal, shift_id)` 固定本台实际末序。同台重复停采复领原末序。停采后可复投旧动作和实收留存队首。空源末序为 0。
- `archive.freeze.seal(ledger, sources, shift_id, endmarks)` 用清册两台的持久末序封报。`sources` 是按设备号索引的记录器字典。班报按清册及源序展开正式实收，列出截至末序的未实收闭区间。已正式提交且待释放的原件计入实收。相同有效末序复领首份；后来补收可单独判级，历史班报保留封报时的条目和缺口。
- `archive.readback.retrieve(ledger, report_id)` 只读取站库。未知报号返回 `None`，已知快照的引用缺失或身份不一致报错。

`ValueError` 表示非法输入、错绑、冲突或队首变化。`LookupError` 表示未知班次或首执。`RuntimeError` 表示持久证据异常。SQLite 和文件系统故障沿原异常传递。实收入口接受可选的 `before_commit`、`before_release`、`after_release` 检查点回调，封报接受 `before_commit`。提交前回调在事务内执行；回调抛错时事务回滚。释放后回调在源提交之后执行，可用于演练失答。检查点回调在实收与封报中分别接收首执和档案记录。
