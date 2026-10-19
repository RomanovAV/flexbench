import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def path(value):
    return "/".join("m:" + item for item in value.split("/"))


def text(node, key):
    return node.findtext(path(key), default="", namespaces=NS).strip()


def dependencies(node, key):
    return node.findall(path(key + "/dependency"), NS)


def main():
    workspace = Path(sys.argv[1])
    try:
        root = ET.parse(workspace / "pom.xml").getroot()
        child = ET.parse(workspace / "api/pom.xml").getroot()
    except (ET.ParseError, OSError) as error:
        print(json.dumps({"checks": [{"id": "xml", "passed": False}], "detail": str(error)}))
        return
    checks = []

    def check(key, passed):
        checks.append({"id": key, "passed": bool(passed)})

    check("xml", True)
    check("root_coordinates", text(root,"groupId")=="org.example" and text(root,"artifactId")=="pilot-parent"
          and text(root,"version")=="1.0.0" and text(root,"packaging")=="pom")
    check("module_parent", [m.text for m in root.findall(path("modules/module"), NS)]==["api"]
          and text(child,"parent/groupId")=="org.example" and text(child,"parent/artifactId")=="pilot-parent"
          and text(child,"parent/version")=="1.0.0" and text(child,"artifactId")=="api"
          and text(child,"version") in ("", "1.0.0") and text(child,"groupId") in ("", "org.example")
          and text(child,"parent/relativePath") in ("", "../pom.xml"))
    check("single_version_source", text(root,"properties/framework.version")=="3.2.1"
          and child.find(path("properties/framework.version"), NS) is None)
    managed = dependencies(root,"dependencyManagement/dependencies")
    direct = dependencies(child,"dependencies")
    core = [d for d in managed if text(d,"groupId")=="org.example.framework" and text(d,"artifactId")=="framework-core"]
    http = [d for d in direct if text(d,"groupId")=="org.example.framework" and text(d,"artifactId")=="framework-http"]
    managed_http = [d for d in managed if text(d,"groupId")=="org.example.framework" and text(d,"artifactId")=="framework-http"]
    check("core_managed", len(core)==1 and text(core[0],"version")=="${framework.version}"
          and text(core[0],"scope") in ("", "compile"))
    check("http_aligned", len(http)==1 and (text(http[0],"version")=="${framework.version}"
          or (not text(http[0],"version") and len(managed_http)==1 and text(managed_http[0],"version")=="${framework.version}"))
          and text(http[0],"scope") in ("", "compile"))
    utility = [d for d in direct if text(d,"groupId")=="org.example" and text(d,"artifactId")=="utility"]
    check("utility_preserved", len(utility)==1 and text(utility[0],"version")=="5.0.0" and text(utility[0],"scope") in ("", "compile"))
    check("no_extra_dependencies", len(direct)==2 and len(managed)==1+len(managed_http) and len(managed_http)<=1
          and sum(len(node.findall(".//m:dependency",NS)) for node in (root,child))==len(direct)+len(managed))
    check("all_managed_versions_aligned", all(text(d,"version")=="${framework.version}" for d in managed))
    check("no_plugins_repositories", all(not node.findall(".//m:plugin",NS) and not node.findall(".//m:repository",NS)
          and not node.findall(".//m:pluginRepository",NS) for node in (root,child)))
    print(json.dumps({"checks": checks}))


if __name__ == "__main__":
    main()
