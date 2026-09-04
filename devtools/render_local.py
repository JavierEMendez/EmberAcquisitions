"""Render the report locally so layout can be checked without a round trip.

WeasyPrint needs GTK and will not install on Windows, so every page-break
change was a hypothesis someone else had to test by exporting. Chrome is
already on this machine and honours the same @page, break-inside and
break-after properties, which is what these bugs live in. It is not
byte-identical to WeasyPrint -- fonts and hyphenation differ -- but a section
that strands its heading here strands it there.

    python devtools/render_local.py out.pdf
"""
import io
import json
import os
import subprocess
import sys
import tempfile

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# run from devtools/, import from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_html():
    import jinja2
    import acq_gis
    import acq_parcels as pc
    import acq_report
    from shapely.geometry import shape as shp_shape
    from shapely.ops import unary_union

    here = os.path.dirname(os.path.abspath(__file__))
    fixture = os.path.join(here, "report_fixture.json")
    data = json.load(io.open(fixture, encoding="utf-8"))

    r = pc.find_parcels_by_owner(data["owner"], include_geometry=True)
    ps = [p for p in (r.get("parcels") or []) if p.get("geometry")]
    proj = {"name": data["name"],
            "tracts": [{"prop_id": str(p["prop_id"]), "owner_name": p.get("owner_name"),
                        "acres": p.get("acres"), "county": p.get("county"),
                        "geometry": p["geometry"]} for p in ps]}
    analysis = acq_gis.run_analysis(proj)
    ctx = acq_report.build_context(proj, analysis, data["payloads"], data["elevation"])
    try:
        u = unary_union([shp_shape(t["geometry"]) for t in proj["tracts"]])
        ctx["site_map"] = acq_report.render_site_map(
            u, analysis.get("constraint_geoms"), proj["tracts"])
    except Exception as e:
        print("site map skipped:", e)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True)
    return env.get_template("acq_report.html").render(r=ctx)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "report_local.pdf"
    html = build_html()
    tmp = os.path.join(tempfile.gettempdir(), "_acq_report_local.html")
    io.open(tmp, "w", encoding="utf-8").write(html)
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out}", "file:///" + tmp.replace("\\", "/")],
                   check=True, capture_output=True, timeout=180)
    print(f"rendered {len(html):,} chars -> {out}")


if __name__ == "__main__":
    main()
