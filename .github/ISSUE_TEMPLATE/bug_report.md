---
name: Bug Report
about: Report a bug in ROCK framework
title: '[BUG] '
labels: 'bug'
assignees: ''

---

**Issue Type**
bug

**Bug Description**
A clear and concise description of what the bug is.

**Steps to Reproduce**
Steps to reproduce the behavior:
1. Start ROCK admin server with `rock admin start`
2. Create environment with `rock.make("game:Sokoban-v0-easy")`
3. Execute action `env.step("\\boxed{up}")`
4. Observe error

**Expected Behavior**
A clear and concise description of what you expected to happen.
Example: The environment should return observation, reward, terminated, truncated, info tuple.

**Actual Behavior**
A clear and concise description of what actually happened.
Example: The environment crashed with ConnectionError or returned malformed response.

**Error Logs**
If applicable, paste relevant error logs or stack traces:
```
Paste error logs here
```

**Environment Information**
- **OS**: [e.g. Ubuntu 22.04, macOS 13.0, Windows 11]
- **Python Version**: [e.g. 3.11.5]
- **ROCK Version**: [e.g. 0.2.0]
- **Installation Method**: [e.g. pip install rl-rock, source installation with uv]
- **Docker Version**: [e.g. 24.0.6] (if using sandbox features)
- **Deployment Type**: [e.g. local, distributed, ray]

**ROCK Configuration**
- **Runtime Environment Type**: [e.g. uv, pip, conda]
- **Sandbox Image**: [e.g. python:3.11, custom image]
- **Resource Allocation**: [e.g. memory=8g, cpus=2.0]

**Component Affected**
- [ ] SDK (Environment/Sandbox client)
- [ ] Admin Server
- [ ] Worker Node
- [ ] Rocklet Service
- [ ] CLI Tool
- [ ] Environment Protocol (GEM/Bash/Chat)
- [ ] Docker Integration
- [ ] Other: ___________

**Additional Context**
Add any other context about the problem here, such as:
- Related environment configurations
- Network setup (if using distributed deployment)
- Custom environment implementations
- Integration with specific RL frameworks