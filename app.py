from pathlib import Path
from flask import Flask, render_template, abort

app = Flask(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _discover_pages() -> list[dict[str, str]]:
    pages = []
    for template in sorted(TEMPLATE_DIR.rglob("*.html")):
        rel = template.relative_to(TEMPLATE_DIR).as_posix()
        if rel in {"base.html", "sidebar.html", "topbar.html"} or rel.startswith("inspections/partials/"):
            continue
        route = "/" if rel == "home.html" else "/" + rel.removesuffix(".html")
        title = rel.removesuffix(".html").replace("_", " ").replace("/", " / ").title()
        pages.append({"route": route, "template": rel, "title": title})
    return pages


PAGES = _discover_pages()
NAV_LINKS = [{"label": page["title"], "url": page["route"]} for page in PAGES]

for page in PAGES:
    def _view(t=page["template"], title=page["title"]):
        return render_template(t, page_title=title, nav_links=NAV_LINKS)

    endpoint = "page_" + page["route"].strip("/").replace("/", "_").replace("-", "_")
    endpoint = endpoint or "page_home"
    app.add_url_rule(page["route"], endpoint, _view)


@app.errorhandler(404)
def not_found(_err):
    return render_template("home.html", page_title="Not Found", nav_links=NAV_LINKS), 404


if __name__ == "__main__":
    app.run(debug=True)
