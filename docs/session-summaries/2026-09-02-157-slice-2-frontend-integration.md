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

At code/evidence head `1007cfafd8baabb45da69e0c0e104df2da70cef1`, under
plan authority `27823eef6a872901add29a8f251429641680402b` (D185 and D186),
the parent ran:

```bash
./.venv/bin/python scripts/acceptance_first_crack_wheel.py --local-model-dir /private/tmp/coffee-roaster-mcp-157-artifacts
```

It passed for `coffee_roaster_mcp-0.1.16-py3-none-any.whl`, using
`syamaner/coffee-first-crack-detection` at pinned revision
`b349a919c34b6130472da97c01817be404e4f629`. Confirmation after beans added
was `16.019406124966203`, inside the inclusive
`3.82710390663442..21.0` interval and strictly below `20.017`. The confirming
confidence was `0.9845320744703858` from `fc_window_confirmed_row`, sequence
`2`: exactly one confirmed row among three current-session rows (minimum
`0.6`). Onset-candidate payload and summary confidence were both
`0.8032823849602806`, diagnostic-only. Observed onset after T0 was
`0.019406124966204197`, print-only and non-gating. Replay metrics were three
emitted, three processed, and zero dropped windows. Wheel metadata had no
banned requirements; installed banned distributions were empty, and Torch,
Torchaudio, and Transformers import specs were all false. Historical recorded
time `10.017558290999885` and confidence `0.7762153826546956` remain print-only
and ungated.

Local evidence: focused acceptance tests passed 48 tests; the full
branch-coverage suite passed 670 tests at `92.06%` (required `90%`); Ruff
check/format passed; Pyright reported zero errors; and the CLI reported
`0.1.16`. Source/wheel build, ordinary clean-wheel smoke, and this dedicated
acceptance all passed.

This is hardware-free package/software readiness only. It does not establish
Pi, hardware, #194, threshold, or release acceptance.
