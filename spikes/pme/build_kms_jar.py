"""Compile LocalKmsClient against the Parquet/Hadoop jars that ship inside PySpark.

Deliberately compiles against PySpark's own jars rather than resolving Parquet from
Maven, so the KMS client can never be built against a different Parquet version than
the one Spark will load at runtime -- a mismatch there surfaces as an
``AbstractMethodError`` deep inside a Spark task, which is miserable to diagnose.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "java" / "LocalKmsClient.java"
BUILD = HERE / "build"
JAR_PATH = HERE / "confidentia-localkms.jar"


def find_java_home() -> Path:
    """Locate a JDK 17 installation.

    Spark 3.5 supports Java 8/11/17 only. This machine also has JDK 22 installed and
    on PATH, which Spark rejects, so the JDK is resolved explicitly rather than
    inherited from the ambient environment.
    """
    explicit = os.environ.get("CONFIDENTIA_JAVA_HOME") or os.environ.get("JAVA_HOME")
    if explicit and (Path(explicit) / "bin").exists():
        return Path(explicit)

    candidates: list[Path] = []
    for base in (
        Path("C:/Program Files/Eclipse Adoptium"),
        Path("C:/Program Files/Java"),
        Path("/usr/lib/jvm"),
    ):
        if base.exists():
            candidates.extend(p for p in base.iterdir() if p.is_dir() and "17" in p.name)
    if not candidates:
        raise SystemExit(
            "No JDK 17 found. Install Temurin 17 or set CONFIDENTIA_JAVA_HOME."
        )
    return sorted(candidates)[-1]


def find_spark_jars() -> Path:
    import pyspark

    jars = Path(pyspark.__file__).parent / "jars"
    if not jars.exists():
        raise SystemExit(f"PySpark jars directory not found at {jars}")
    return jars


def main() -> int:
    java_home = find_java_home()
    jars_dir = find_spark_jars()
    javac = java_home / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    jar_tool = java_home / "bin" / ("jar.exe" if os.name == "nt" else "jar")

    print(f"JDK        : {java_home}")
    print(f"Spark jars : {jars_dir}")

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    classpath = str(jars_dir / "*")
    compile_cmd = [
        str(javac),
        "-nowarn",
        "-source", "17", "-target", "17",
        "-cp", classpath,
        "-d", str(BUILD),
        str(SRC),
    ]
    result = subprocess.run(compile_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("javac failed:\n" + result.stdout + result.stderr, file=sys.stderr)
        return result.returncode

    jar_cmd = [str(jar_tool), "--create", "--file", str(JAR_PATH), "-C", str(BUILD), "."]
    result = subprocess.run(jar_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("jar failed:\n" + result.stdout + result.stderr, file=sys.stderr)
        return result.returncode

    print(f"Built      : {JAR_PATH}  ({JAR_PATH.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
