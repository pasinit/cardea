"""Dependency metadata only; never inspect environment variables or configuration."""

import importlib.metadata
import json

print(
    json.dumps(
        sorted(
            (
                {
                    "name": dist.metadata["Name"],
                    "version": dist.version,
                    "license_expression": dist.metadata.get("License-Expression"),
                    "license_classifiers": [
                        value
                        for value in dist.metadata.get_all("Classifier", [])
                        if value.startswith("License ::")
                    ],
                    "license_files": [
                        str(path)
                        for path in (dist.files or [])
                        if "license" in str(path).lower() or "copying" in str(path).lower()
                    ],
                }
                for dist in importlib.metadata.distributions()
            ),
            key=lambda row: row["name"].lower(),
        ),
        indent=2,
    )
)
