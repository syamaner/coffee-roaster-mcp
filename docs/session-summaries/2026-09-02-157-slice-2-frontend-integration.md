# #157 slice 2: MCP-owned frontend integration

This software-readiness slice replaces the released ONNX detector's default
Transformers AST feature extraction with the MCP-owned NumPy/SciPy
`MelFrontend`, removes the Transformers runtime dependency, and adds a
parent-runnable clean-wheel acceptance path. It does not validate Pi, audio
hardware, Hottop behaviour, package publication, or release readiness.

The current published package remains `v0.1.16`. Issue #157 remains open for
Pi and combined acceptance, while #194 remains open and unstarted. The intended
`0.2.0` minor is not publication authority.

## Exact-head clean-wheel acceptance

At head `180cc6fec2fd6d7833d09072c9fce9342b7ad284`, under D185 plan authority
`7e4faa2d7b2a851578d971db004dedcbaf62d4ef`, the parent ran:

```bash
./.venv/bin/python scripts/acceptance_first_crack_wheel.py --local-model-dir /private/tmp/coffee-roaster-mcp-157-artifacts
```

It passed for `coffee_roaster_mcp-0.1.16-py3-none-any.whl`, using
`syamaner/coffee-first-crack-detection` at pinned revision
`b349a919c34b6130472da97c01817be404e4f629`. Confirmation after beans added
was `16.01871283299795`, inside the inclusive
`3.82710390663442..21.0` interval and strictly below `20.017`. Confirming and
payload confidence were both `0.8032823849602806` (minimum `0.6`); observed
onset `0.018712832997947904` is print-only and non-gating. Replay metrics were
three emitted, three processed, and zero dropped windows. Wheel metadata had no
banned requirements; installed banned distributions were empty, and Torch,
Torchaudio, and Transformers import specs were all false.

This is hardware-free package/software readiness only. It does not establish
Pi, hardware, #194, threshold, or release acceptance.
