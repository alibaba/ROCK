import re

def parse_memory_size(size_str: str) -> int:
    size_str = size_str.strip().lower()
    units = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
    }
    
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)?$", size_str)
    if not match:
        raise ValueError(f"Invalid memory size format: {size_str}")
    number = float(match.group(1))
    unit = match.group(2) or "b"
    if unit not in units:
        raise ValueError(f"Unknown memory unit: {unit}")
    return int(number * units[unit])
