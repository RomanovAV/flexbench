import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "cases/service-launch-contract/fixture"
IMAGE = "registry.example/java17:17.0.13"
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def xml_shape(node):
    return node.tag, (node.text or "").strip(), sorted(node.attrib.items()), [xml_shape(child) for child in node]


def main():
    workspace = Path(sys.argv[1]).resolve()
    checks = []

    def check(key, function):
        try:
            checks.append({"id": key, "passed": bool(function())})
        except Exception:
            checks.append({"id": key, "passed": False})

    def docker_image():
        lines = [line.strip() for line in (workspace / "Dockerfile").read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
        args = [line.split(None, 1)[1] for line in lines if line.upper().startswith("ARG ")]
        return args == ["RUNTIME_IMAGE=" + IMAGE]

    check("docker_runtime_image", docker_image)

    def docker_preserved():
        def rest(path):
            return [shlex.split(line) for line in path.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith('#') and not line.strip().upper().startswith('ARG ')]
        return rest(workspace / "Dockerfile") == rest(FIXTURE / "Dockerfile")

    check("docker_stages_and_runtime_preserved", docker_preserved)

    def pom_image():
        root = ET.parse(workspace / "pom.xml").getroot()
        return root.findtext("m:properties/m:runtime.image", namespaces=NS) == IMAGE

    check("jib_runtime_image", pom_image)

    def pom_preserved():
        roots = [ET.parse(path).getroot() for path in (workspace / "pom.xml", FIXTURE / "pom.xml")]
        for root in roots:
            prop = root.find("m:properties/m:runtime.image", NS)
            if prop is None:
                return False
            prop.text = "IGNORED_IMAGE"
        return xml_shape(roots[0]) == xml_shape(roots[1])

    check("maven_configuration_preserved", pom_preserved)

    def env_image():
        values = [line.strip() for line in (workspace / "deploy.env").read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
        return sorted(values) == sorted(["RUNTIME_IMAGE=" + IMAGE, "APP_PORT=8080"])

    check("deployment_image_and_port", env_image)
    with tempfile.TemporaryDirectory(prefix="flexbench-launch-") as directory:
        root = Path(directory)
        stub = root / "fake java"
        stub.write_text("#!" + sys.executable + "\nimport json,os,sys\nprint(json.dumps({'argv':sys.argv[1:],'pid':os.getpid()}))\nsys.exit(int(os.environ.get('STUB_EXIT','0')))\n")
        stub.chmod(0o700)
        (root / "java").write_bytes(stub.read_bytes())
        (root / "java").chmod(0o700)

        def launch(extra=None, args=()):
            env = {k: v for k, v in os.environ.items() if k not in ("JAVA_BIN", "APP_JAR", "JAVA_HEAP_MB", "STUB_EXIT")}
            env.update(PATH=str(root) + os.pathsep + env.get("PATH", ""), JAVA_BIN=str(stub))
            env.update(extra or {})
            with subprocess.Popen(["/bin/sh", str(workspace / "entrypoint.sh"), *args], cwd=root, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                try:
                    out, err = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
                return process.returncode, out, err, process.pid

        def matches(extra, args, expected, code=0, pid=False):
            actual, out, _, process_id = launch(extra, args)
            record = json.loads(out)
            return actual == code and record["argv"] == expected and (not pid or record["pid"] == process_id)

        check("default_launch", lambda: matches({"JAVA_BIN": ""}, (), ["-jar", "/opt/service/app.jar"]))
        check("empty_environment_defaults", lambda: matches({"JAVA_BIN": "", "APP_JAR": "", "JAVA_HEAP_MB": ""}, (), ["-jar", "/opt/service/app.jar"]))
        check("quoted_paths_and_arguments", lambda: matches({"APP_JAR": "/tmp/service with space.jar", "JAVA_HEAP_MB": "64"},
              ("one argument", "", "*", "--flag=a b"), ["-Xmx64m", "-jar", "/tmp/service with space.jar", "one argument", "", "*", "--flag=a b"]))
        check("heap_optional", lambda: matches({"APP_JAR": "a.jar"}, (), ["-jar", "a.jar"]))

        def invalid_heap():
            for value in ("0", "-1", "01", "1.5", " 64", "64 ", "1m", "$(touch injected)", "1;touch injected", "\n4"):
                code, out, _, _ = launch({"JAVA_HEAP_MB": value})
                if code != 2 or out.strip():
                    return False
            return not (root / "injected").exists()

        check("invalid_heap_before_launch", invalid_heap)
        check("heap_positive_digits", lambda: all(matches({"JAVA_HEAP_MB": value}, (), ["-Xmx" + value + "m", "-jar", "/opt/service/app.jar"])
                                                  for value in ("1", "1024", "999999999999999999999")))
        check("literal_arguments_no_eval", lambda: matches({"APP_JAR": "$(touch injected)"}, ("; touch injected", "$(touch injected)",),
              ["-jar", "$(touch injected)", "; touch injected", "$(touch injected)"]) and not (root / "injected").exists())
        check("exec_process_identity", lambda: matches({}, (), ["-jar", "/opt/service/app.jar"], pid=True))
        check("exit_code_preserved", lambda: matches({"STUB_EXIT": "23"}, (), ["-jar", "/opt/service/app.jar"], code=23))
    print(json.dumps({"checks": checks}))


if __name__ == "__main__":
    main()
