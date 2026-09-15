# 发布与验签

正式产物只由 `.github/workflows/release.yml` 在 `v*` tag 上构建。普通 CI 只有 `contents: read`；发布工作流把构建、OIDC attestation 和 GitHub Release 写入拆成三个 job。只有 attestation job 获得 `id-token: write`/`attestations: write`，只有 publish job 获得 `contents: write`。

每个正式发布包含：

- wheel 与 source distribution；
- `agent-os.cdx.json`（CycloneDX 1.6），记录完整 `uv.lock` 依赖、发行产物哈希、仓库和源提交；
- `SHA256SUMS`；
- GitHub OIDC 签名的 build-provenance attestation。

## 验证步骤

在空目录下载 GitHub Release 资产后执行：

```bash
shasum -a 256 -c SHA256SUMS
gh attestation verify graph_engineering_agent_os-*.whl --repo Feahter/agent-OS
gh attestation verify graph_engineering_agent_os-*.tar.gz --repo Feahter/agent-OS
gh attestation verify agent-os.cdx.json --repo Feahter/agent-OS
```

随后检查 SBOM：

```bash
python3 - <<'PY'
import json

sbom = json.load(open("agent-os.cdx.json", encoding="utf-8"))
properties = sbom["metadata"]["component"]["properties"]
commit = next(item["value"] for item in properties if item["name"] == "agent-os:source-commit")
print(commit)
PY
```

输出必须等于该 release tag 指向的提交。`gh attestation verify` 同时核对 GitHub 仓库身份、OIDC 签名和 subject digest；`SHA256SUMS` 用于发现下载或存储损坏。任一检查失败、SBOM 缺失、来源仓库不符或 tag/提交不符时，不要安装，并按 [SECURITY.md](../SECURITY.md) 报告。

`agent-os agent-os verify-release` 校验的是 Agent OS 自包含状态发行目录的内部清单和恢复可重复性，不等同于上述外部来源签名。对外分发仍必须走 tag 发布工作流。
