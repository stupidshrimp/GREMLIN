from flask import Flask, render_template

app = Flask(__name__)

PAGES = [
    {"route": "/", "template": "home.html", "title": "Home", "icon": "🏠"},
    {
        "route": "/life-data-analysis",
        "template": "life_data_analysis.html",
        "title": "Life Data Analysis",
        "icon": "📈",
    },
    {"route": "/metrics", "template": "metrics.html", "title": "Metrics", "icon": "📊"},
    {
        "route": "/standards-and-documentation",
        "template": "standards_and_documentation.html",
        "title": "Standards and Documentation",
        "icon": "📚",
    },
    {"route": "/settings", "template": "settings.html", "title": "Settings", "icon": "⚙️"},
]

NAV_LINKS = [
    {"label": page["title"], "url": page["route"], "icon": page["icon"]}
    for page in PAGES
]

for page in PAGES:

    def _view(t=page["template"], title=page["title"]):
        return render_template(t, page_title=title, nav_links=NAV_LINKS)

    endpoint = "page_" + ("home" if page["route"] == "/" else page["route"].strip("/").replace("-", "_"))
    app.add_url_rule(page["route"], endpoint, _view)


@app.errorhandler(404)
def not_found(_err):
    return render_template("home.html", page_title="Not Found", nav_links=NAV_LINKS), 404


if __name__ == "__main__":
    app.run(debug=True)
