# 贡献指南

感谢参与 Agent OS。

1. 先通过 Issue 描述问题或提案，较大改动请先对齐设计边界。
2. 从独立分支提交小而清晰的改动。
3. 不得提交密钥、模型输入/输出、用户数据或运行态目录。
4. 提交前运行：

   ```bash
   python3 -m pytest -q
   PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m compileall -q grapheng tests
   ```

5. 提交信息使用 `type(scope): description` 格式。

新增行为应包含测试，并保持 Python 3.9 兼容和零第三方运行时依赖。
