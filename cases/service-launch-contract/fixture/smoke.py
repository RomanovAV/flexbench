import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as directory:
    stub = Path(directory) / "fake java"
    stub.write_text('#!' + sys.executable + '\nimport sys\nprint(repr(sys.argv[1:]))\n')
    stub.chmod(0o700)
    env = dict(os.environ, JAVA_BIN=str(stub), APP_JAR='/tmp/service with space.jar', JAVA_HEAP_MB='64')
    result = subprocess.run(['/bin/sh', str(root / 'entrypoint.sh'), 'one argument', ''], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(['-Xmx64m', '-jar', '/tmp/service with space.jar', 'one argument', ''])
print('Smoke passed')
