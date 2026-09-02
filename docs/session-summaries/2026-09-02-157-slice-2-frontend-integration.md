# #157 slice 2: MCP-owned frontend integration

This software-readiness slice replaces the released ONNX detector's default
Transformers AST feature extraction with the MCP-owned NumPy/SciPy
`MelFrontend`, removes the Transformers runtime dependency, and adds a
parent-runnable clean-wheel acceptance path. It does not validate Pi, audio
hardware, Hottop behaviour, package publication, or release readiness.

The current published package remains `v0.1.16`. Issue #157 remains open for
Pi and combined acceptance, while #194 remains open and unstarted. The intended
`0.2.0` minor is not publication authority.
