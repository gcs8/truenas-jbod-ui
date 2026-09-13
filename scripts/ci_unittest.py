#!/usr/bin/env python3
"""Trial-only needs-result check. No discovery, partitions, or CI routing."""
import argparse
import json
import os


def all_success(needs):
    return (isinstance(needs, dict) and set(needs) == {'python-tests'}
            and isinstance(needs['python-tests'], dict)
            and needs['python-tests'].get('result') == 'success')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-needs', action='store_true', required=True)
    parser.parse_args(argv)
    try:
        needs = json.loads(os.environ.get('CI_NEEDS', '{}'))
    except (TypeError, ValueError):
        return 1
    return 0 if all_success(needs) else 1


if __name__ == '__main__':
    raise SystemExit(main())
