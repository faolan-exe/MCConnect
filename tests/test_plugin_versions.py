"""The addons are built against the MCDataLink of this repository (java plugin/addons/pom.xml). If its version is not
the one in MCDataLink/pom.xml, the Docker build fails: Maven looks for the old jar in the public repositories."""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "java plugin")


def own_version(pom):
    """The project's own <version> (the first one after the artifactId, not a dependency's)."""
    text = open(os.path.join(ROOT, pom), encoding="utf-8").read()
    return re.search(r"<artifactId>MCDataLink</artifactId>\s*<version>([^<]+)</version>", text).group(1)


def test_addons_depend_on_the_current_plugin_version():
    plugin = own_version("MCDataLink/pom.xml")
    addons = open(os.path.join(ROOT, "addons/pom.xml"), encoding="utf-8").read()
    dependency = re.search(r"<artifactId>MCDataLink</artifactId>\s*<version>([^<]+)</version>", addons).group(1)
    assert dependency == plugin, f"java plugin/addons/pom.xml needs MCDataLink {plugin}, not {dependency}"
