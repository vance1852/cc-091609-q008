# 中药调剂称量防错

该项目记录处方药味、药斗身份、电子秤事件和双人复核。读数、去皮及回退均保留发生顺序，设备校准状态随称量记录保存。

基础结构位于 `dispensing/contracts.py`，`fixtures/weighing_session.json` 还原一次脱敏的换秤与错斗过程。项目面向 Python 3.11，可使用 `python -m compileall dispensing` 检查语法。
