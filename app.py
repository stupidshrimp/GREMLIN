from flask import Flask, render_template

app = Flask(__name__)

ICONS = {
    "home": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.2 3.7 10A1 1 0 0 0 3.3 11.1a1 1 0 0 0 1 .7h1v7.3c0 .5.4.9.9.9h4.8v-5.3c0-.5.4-.9.9-.9h1.8c.5 0 .9.4.9.9V20h4.8c.5 0 .9-.4.9-.9v-7.3h1a1 1 0 0 0 .6-1.8L12 3.2Z"/></svg>',
    "trend": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.7 18.9a1 1 0 0 1 0-1.4l5.6-5.6c.4-.4 1-.4 1.4 0l2.7 2.7 4.9-4.9h-2.4a1 1 0 1 1 0-2h4.8c.5 0 .9.4.9.9v4.8a1 1 0 1 1-2 0V11l-5.6 5.6c-.4.4-1 .4-1.4 0l-2.7-2.7-4.9 4.9a1 1 0 0 1-1.4 0Z"/><path d="M3 5.1c0-.5.4-.9.9-.9h16.2c.5 0 .9.4.9.9s-.4.9-.9.9H3.9A.9.9 0 0 1 3 5.1Z"/></svg>',
    "chart": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 20.8a1 1 0 0 1-1-1V4.2a1 1 0 1 1 2 0v14.6h14.6a1 1 0 1 1 0 2H4.5Z"/><path d="M8.1 16.1a1 1 0 0 1-1-1v-3.4a1 1 0 1 1 2 0v3.4a1 1 0 0 1-1 1Zm4 0a1 1 0 0 1-1-1V8.6a1 1 0 1 1 2 0v6.5a1 1 0 0 1-1 1Zm4 0a1 1 0 0 1-1-1v-5a1 1 0 1 1 2 0v5a1 1 0 0 1-1 1Z"/></svg>',
    "docs": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 2.8h7.8c.2 0 .5.1.6.3l3.8 3.8c.2.2.3.4.3.6v13.7c0 .5-.4.9-.9.9H6c-.5 0-.9-.4-.9-.9V3.7c0-.5.4-.9.9-.9Zm7.2 1.9v2.6c0 .5.4.9.9.9h2.6L13.2 4.7ZM8.2 11.2c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Zm0 3.8c0-.5.4-.9.9-.9h5.8a1 1 0 1 1 0 2H9.1a.9.9 0 0 1-.9-.9Z"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 2.9a1 1 0 0 1 2 0v1.3a7.8 7.8 0 0 1 2.1.9l.9-.9a1 1 0 1 1 1.4 1.4l-.9.9c.4.6.7 1.4.9 2.1h1.3a1 1 0 1 1 0 2h-1.3a7.8 7.8 0 0 1-.9 2.1l.9.9a1 1 0 1 1-1.4 1.4l-.9-.9c-.6.4-1.4.7-2.1.9v1.3a1 1 0 1 1-2 0v-1.3a7.8 7.8 0 0 1-2.1-.9l-.9.9a1 1 0 1 1-1.4-1.4l.9-.9a7.8 7.8 0 0 1-.9-2.1H3.6a1 1 0 1 1 0-2h1.3c.2-.8.5-1.5.9-2.1l-.9-.9A1 1 0 0 1 6.3 4l.9.9c.6-.4 1.4-.7 2.1-.9V2.9Zm1 5.1a3.8 3.8 0 1 0 0 7.7 3.8 3.8 0 0 0 0-7.7Z"/></svg>',
}

PAGES = [
    {"route": "/", "template": "home.html", "title": "Home", "icon": ICONS["home"]},
    {
        "route": "/life-data-analysis",
        "template": "life_data_analysis.html",
        "title": "Life Data Analysis",
        "icon": ICONS["trend"],
    },
    {"route": "/metrics", "template": "metrics.html", "title": "Metrics", "icon": ICONS["chart"]},
    {
        "route": "/standards-and-documentation",
        "template": "standards_and_documentation.html",
        "title": "Standards and Documentation",
        "icon": ICONS["docs"],
    },
    {"route": "/settings", "template": "settings.html", "title": "Settings", "icon": ICONS["settings"]},
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
