#!/usr/bin/env python3
"""Build-time processor: apps-prod.yaml → apps.yaml.

Strips keys that should not appear in production:
  - disable: true   (all apps go active)
  - debug_preserve_run_dirs: true  (dev-only debug flag)

Uses PyYAML (available in base image) with !secret tag preservation.
"""

import sys
import yaml


# Preserve !secret tags through round-trip
class SecretTag:
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return f"!secret {self.value}"


def secret_constructor(loader, node):
    return SecretTag(loader.construct_scalar(node))


def secret_representer(dumper, data):
    return dumper.represent_scalar("!secret", data.value)


yaml.SafeLoader.add_constructor("!secret", secret_constructor)
yaml.add_representer(SecretTag, secret_representer)

# Keys to strip from each app config
STRIP_KEYS = {"disable", "debug_preserve_run_dirs"}


def process(input_path: str, output_path: str) -> None:
    with open(input_path, "r") as f:
        data = yaml.load(f, Loader=yaml.SafeLoader)

    if not isinstance(data, dict):
        print(f"ERROR: Expected mapping at YAML root: {input_path}", file=sys.stderr)
        sys.exit(1)

    for app_name, app_config in data.items():
        if isinstance(app_config, dict):
            for key in STRIP_KEYS:
                app_config.pop(key, None)

    with open(output_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    stripped = ", ".join(sorted(STRIP_KEYS))
    print(f"Processed {input_path} → {output_path} (stripped: {stripped})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.yaml> <output.yaml>", file=sys.stderr)
        sys.exit(1)
    process(sys.argv[1], sys.argv[2])
