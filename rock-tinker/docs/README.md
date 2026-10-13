# Tinker API documentation

For project setup, runnable commands and parameter meanings, see the [SDK README](../README.md). This directory indexes the generated Python API documentation.

## API reference

- [TinkerClient](api/tinkerclient.md)
- [TrainingClient](api/trainingclient.md)
- [SamplingClient](api/samplingclient.md)
- [REST client](api/restclient.md)
- [API futures](api/apifuture.md)
- [Types](api/types.md)
- [Exceptions](api/exceptions.md)

## Regenerate API documentation

From the SDK source directory, run:

```bash
cd /path/to/ROCK/rock-tinker
uv run --script scripts/generate_docs.py
```

The script reads Python docstrings and writes API reference pages. Review and commit the generated changes when updating an API.
