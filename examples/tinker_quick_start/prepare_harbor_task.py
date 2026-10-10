"""Copy a public Harbor task without altering its environment or verifier."""
import argparse
from pathlib import Path
import shutil


def prepare(source, target):
    source, target = Path(source), Path(target)
    for name in ('instruction.md', 'task.toml', 'environment', 'tests'):
        if not (source / name).exists():
            raise ValueError('Incomplete Harbor task: ' + name)
    output = target / source.name
    if output.exists():
        raise ValueError('Refusing to overwrite existing task: ' + str(output))
    shutil.copytree(source, output, copy_function=shutil.copy2)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    args = parser.parse_args()
    print(prepare(args.source, args.target))
