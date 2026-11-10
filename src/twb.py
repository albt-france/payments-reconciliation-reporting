"""
Tiny writer for Tableau workbooks (.twb = XML), used by build_tableau.py.

Field references are written "<derivation>:<field>:<type>", as Tableau does internally:
   derivation  none (dimension), sum, avg, min, max, ctd (count distinct), usr (calculated field),
               tmn (date truncated to month), mn (month part)
   type        nk nominal (discrete), ok ordinal (discrete, ordered), qk quantitative (continuous)
e.g. "sum:revenue_eur:qk" = SUM(revenue_eur) as a continuous measure.

What Tableau Public 2026.3 needs (learned by opening the files and reading its error messages):
  - <document-format-change-manifest> and <style> at the top, no <simple-id> elements,
  - <layout> with dim-percentage / measure-percentage on each data source,
  - every data source backed by an extract (.hyper), "Extract"."Extract" table,
  - <device-preview> in dashboard windows.
"""
import re
import xml.etree.ElementTree as ET

DERIVATION = {"none": "None", "sum": "Sum", "avg": "Avg", "min": "Min", "max": "Max", "ctd": "CountD",
              "cnt": "Count", "usr": "User", "tmn": "Month-Trunc", "mn": "Month", "yr": "Year",
              "tdy": "Day-Trunc", "attr": "Attribute"}
TYPE = {"nk": "nominal", "ok": "ordinal", "qk": "quantitative"}
REMOTE_TYPE = {"string": "129", "real": "5", "integer": "20", "date": "133", "datetime": "135", "boolean": "11"}
NEWLINE = "Æ\n"  # Tableau's line break inside formatted text


def sub(parent, tag, **attrs):
    e = ET.SubElement(parent, tag)
    # python names -> XML names:  class_ -> class,  type_v2 -> type-v2,  user__ui_domain -> user:ui-domain
    for k, v in attrs.items():
        if v is not None:
            e.set(k.rstrip("_").replace("__", ":").replace("_", "-"), str(v))
    return e


def split_ref(ref):
    der, field, typ = ref.split(":", 2)
    return der, field, typ


def runs_xml(parent, runs, ds_name=None):
    """runs: list of (text, style dict). '\\n' alone = line break. '{ref}' inside text = a field value."""
    ft = sub(parent, "formatted-text")
    for txt, s in runs:
        if txt == "\n":
            sub(ft, "run").text = NEWLINE
            continue
        if ds_name:
            txt = re.sub(r"\{([^}]+)\}", lambda m: f"<[{ds_name}].[{m.group(1)}]>", txt)
        sub(ft, "run", bold="true" if s.get("bold") else None, fontalignment=s.get("align"),
            fontcolor=s.get("color", "#0b0b0b"), fontname=s.get("font"), fontsize=str(s.get("size", 10))).text = txt
    return ft


class DataSource:
    def __init__(self, name, caption, directory, filename, columns, dims=(), captions=None, formats=None):
        """columns: list of (name, datatype); dims: numeric columns to treat as dimensions."""
        self.name, self.caption, self.directory, self.filename = name, caption, directory, filename
        self.columns = dict(columns)
        self.order = [c for c, _ in columns]
        self.dims = set(dims)
        self.captions = captions or {}
        self.formats = formats or {}
        self.calcs = {}
        self.color_maps = {}  # "none:line:nk" -> {'"Deposit fees"': "#2a78d6", ...}

    def role(self, col):
        dt = self.columns[col]
        if col in self.dims or dt in ("string", "date", "datetime", "boolean"):
            return "dimension", "nominal" if dt in ("string", "boolean") else "ordinal"
        return "measure", "quantitative"

    def calc(self, name, formula, datatype="real", role="measure", type_="quantitative", fmt=None):
        self.calcs[name] = dict(formula=formula, datatype=datatype, role=role, type=type_, fmt=fmt)
        return name

    def dim(self, name, formula, datatype="string", fmt=None):
        t = "nominal" if datatype in ("string", "boolean") else "ordinal"
        return self.calc(name, formula, datatype=datatype, role="dimension", type_=t, fmt=fmt)

    def column_xml(self, parent, col):
        if col in self.calcs:
            c = self.calcs[col]
            e = sub(parent, "column", caption=col, datatype=c["datatype"], default_format=c["fmt"],
                    name=f"[{col}]", role=c["role"], type=c["type"])
            sub(e, "calculation", class_="tableau", formula=c["formula"])
            return e
        role, typ = self.role(col)
        return sub(parent, "column", caption=self.captions.get(col), datatype=self.columns[col],
                   default_format=self.formats.get(col), name=f"[{col}]", role=role, type=typ)

    def xml(self, parent):
        short = self.name.split(".")[-1]
        ds = sub(parent, "datasource", caption=self.caption, inline="true", name=self.name, version="18.1")
        conn = sub(ds, "connection", class_="federated")
        nc = sub(sub(conn, "named-connections"), "named-connection", caption=self.filename.split(".")[0],
                 name=f"textscan.{short}")
        sub(nc, "connection", class_="textscan", directory=self.directory, filename=self.filename,
            password="", server="")
        rel = sub(conn, "relation", connection=f"textscan.{short}", name=self.filename,
                  table=f"[{self.filename.replace('.', '#')}]", type="table")
        cols = sub(rel, "columns", character_set="UTF-8", header="yes", locale="en_US", separator=",")
        for i, c in enumerate(self.order):
            sub(cols, "column", datatype=self.columns[c], name=c, ordinal=str(i))
        mr = sub(conn, "metadata-records")
        for i, c in enumerate(self.order):
            r = sub(mr, "metadata-record", class_="column")
            for tag, val in [("remote-name", c), ("remote-type", REMOTE_TYPE[self.columns[c]]),
                             ("local-name", f"[{c}]"), ("parent-name", f"[{self.filename}]"),
                             ("remote-alias", c), ("ordinal", str(i)), ("local-type", self.columns[c]),
                             ("aggregation", "Sum" if self.role(c)[0] == "measure" else "Count"),
                             ("contains-null", "true")]:
                sub(r, tag).text = val
        sub(ds, "aliases", enabled="yes")
        for c in self.order:  # captions / default formats of plain columns
            if c in self.captions or c in self.formats or c in self.dims:
                self.column_xml(ds, c)
        for c in self.calcs:
            self.column_xml(ds, c)
        # Tableau Public only accepts extract-based data sources: the .hyper written by export_tableau.py
        ex = sub(ds, "extract", count="-1", enabled="true", units="records")
        hc = sub(ex, "connection", access_mode="readonly", author_locale="en_US", class_="hyper",
                 dbname=f"{self.directory}/{self.filename.rsplit('.', 1)[0]}.hyper", default_settings="hyper",
                 schema="Extract", sslmode="", tablename="Extract", update_time="11/10/2026 04:00:00 PM",
                 username="tableau_internal_user")
        sub(hc, "relation", name="Extract", table="[Extract].[Extract]", type="table")
        sub(hc, "refresh", increment_key="", incremental_updates="false")
        # describe the extract's columns: Tableau needs them to open this source later (e.g. on a tab switch)
        emr = sub(hc, "metadata-records")
        for i, c in enumerate(self.order):
            r = sub(emr, "metadata-record", class_="column")
            for tag, val in [("remote-name", c), ("remote-type", REMOTE_TYPE[self.columns[c]]),
                             ("local-name", f"[{c}]"), ("parent-name", "[Extract]"),
                             ("remote-alias", c), ("ordinal", str(i)), ("local-type", self.columns[c]),
                             ("aggregation", "Sum" if self.role(c)[0] == "measure" else "Count"),
                             ("contains-null", "true")]:
                sub(r, tag).text = val
        sub(ds, "layout", dim_ordering="alphabetic", dim_percentage="0.5", measure_ordering="alphabetic",
            measure_percentage="0.4", show_structure="true")
        if self.color_maps:
            sr = sub(sub(ds, "style"), "style-rule", element="mark")
            for ref, mapping in self.color_maps.items():
                enc = sub(sr, "encoding", attr="color", field=f"[{ref}]", type="palette")
                for k, col in mapping.items():
                    sub(sub(enc, "map", to=col), "bucket").text = k
        return ds

    def deps(self, fields):
        """Columns that fields depend on (recursively through calculated fields)."""
        out, todo = [], list(fields)
        while todo:
            c = todo.pop()
            if c in out:
                continue
            if c in self.calcs or c in self.columns:
                out.append(c)
            if c in self.calcs:
                todo += [t for t in re.findall(r"\[([^\]]+)\]", self.calcs[c]["formula"])
                         if t in self.calcs or t in self.columns]
        return out


class Params:
    def __init__(self):
        self.p = {}

    def add(self, name, datatype, value, domain, members=None, rng=None, fmt=None, aliases=None):
        self.p[name] = dict(datatype=datatype, value=value, domain=domain, members=members, rng=rng, fmt=fmt,
                            aliases=aliases or {})

    def column_xml(self, parent, name):
        p = self.p[name]
        typ = "quantitative" if p["datatype"] in ("integer", "real", "date") else "nominal"
        e = sub(parent, "column", caption=name, datatype=p["datatype"], default_format=p["fmt"], name=f"[{name}]",
                param_domain_type=p["domain"], role="measure", type=typ, value=p["value"])
        sub(e, "calculation", class_="tableau", formula=p["value"])
        if p["aliases"]:
            al = sub(e, "aliases")
            for k, v in p["aliases"].items():
                sub(al, "alias", key=k, value=v)
        if p["domain"] == "range":
            lo, hi, step = p["rng"]
            sub(e, "range", granularity=step, max=hi, min=lo)
        if p["domain"] == "list":
            m = sub(e, "members")
            for v in p["members"]:
                sub(m, "member", value=v)
        return e

    def xml(self, parent):
        ds = sub(parent, "datasource", hasconnection="false", inline="true", name="Parameters", version="18.1")
        sub(ds, "aliases", enabled="yes")
        for n in self.p:
            self.column_xml(ds, n)
        return ds


class Sheet:
    """One worksheet.
    rows / cols : lists of field refs.   dual=(ref, mark, color) adds a 2nd measure on a dual axis.
    filters     : (ref, "members", ['"BTC"']) or (ref, "true", None) for a boolean calc.
    tc          : {ref: ("Rows"|"Columns"|"Table"|"Field", ordering_field_ref or None)} table-calc addressing.
    label_runs  : custom label text, '{ref}' = a field value.
    """

    def __init__(self, name, ds, title=None, mark="Automatic", rows=(), cols=(), color=None, size=None,
                 text=None, detail=(), tooltip=(), filters=(), color_map=None, mark_color=None, labels=False,
                 sort=None, sort_dir="DESC", dual=None, tc=None, label_runs=None, fmt=None, axis_titles=None,
                 axis_range=None, context=()):
        self.__dict__.update({k: v for k, v in locals().items() if k != "self"})
        self.tc = tc or {}
        self.fmt = fmt or {}

    def fq(self, ref):
        return f"[{self.ds.name}].[{ref}]"

    def shelf(self, refs):
        """Shelf expression: nested dimensions are joined with ' / ', side-by-side measures with ' + '."""
        if not refs:
            return None
        if len(refs) == 1:
            return self.fq(refs[0])
        op = " + " if all(r.endswith(":qk") for r in refs) else " / "
        return "(" + op.join(self.fq(r) for r in refs) + ")"

    def refs(self):
        r = list(self.rows) + list(self.cols) + list(self.detail) + list(self.tooltip)
        r += [x for x in (self.color, self.size, self.text) if x]
        r += [f[0] for f in self.filters]
        if self.sort:
            r += list(self.sort)
        if self.dual:
            r.append(self.dual[0])
        if self.label_runs:
            for txt, _ in self.label_runs:
                r += re.findall(r"\{([^}]+)\}", txt)
        return list(dict.fromkeys(r))

    def xml(self, parent, params):
        ws = sub(parent, "worksheet", name=self.name)
        if self.title:
            runs_xml(sub(sub(ws, "layout-options"), "title"), [(self.title, dict(bold=True, size=11))])
        table = sub(ws, "table")
        view = sub(table, "view")
        dss = sub(view, "datasources")
        refs = self.refs()
        dep_cols = self.ds.deps([split_ref(r)[1] for r in refs])
        used_params = set()
        for c in dep_cols:
            if c in self.ds.calcs:
                used_params |= set(re.findall(r"\[Parameters\]\.\[([^\]]+)\]", self.ds.calcs[c]["formula"]))
        sub(dss, "datasource", caption=self.ds.caption, name=self.ds.name)
        if used_params:
            sub(dss, "datasource", name="Parameters")
        dd = sub(view, "datasource-dependencies", datasource=self.ds.name)
        for c in sorted(dep_cols):
            self.ds.column_xml(dd, c)
        for r in sorted(refs):
            der, field, typ = split_ref(r)
            ci = sub(dd, "column-instance", column=f"[{field}]", derivation=DERIVATION[der], name=f"[{r}]",
                     pivot="key", type=TYPE[typ])
            if r in self.tc:
                otype, ofield = self.tc[r]
                sub(ci, "table-calc", ordering_field=self.fq(ofield) if ofield else None, ordering_type=otype)
        if used_params:
            dp = sub(view, "datasource-dependencies", datasource="Parameters")
            for p in sorted(used_params):
                params.column_xml(dp, p)
        for ref, kind, value in self.filters:
            f = sub(view, "filter", class_="categorical", column=self.fq(ref),
                    context="true" if ref in self.context else None)
            if kind == "top":  # value = (count expression, order expression)
                count, expr = value
                g = sub(f, "groupfilter", count=count, end="top", function="end", units="records",
                        user__ui_marker="end", user__ui_top_by_field="true")
                o = sub(g, "groupfilter", direction="DESC", expression=expr, function="order")
                sub(o, "groupfilter", function="level-members", level=f"[{ref}]", user__ui_enumeration="all",
                    user__ui_marker="enumerate")
                continue
            members = ["true"] if kind == "true" else value
            if len(members) == 1:
                sub(f, "groupfilter", function="member", level=f"[{ref}]", member=members[0],
                    user__ui_domain="database", user__ui_enumeration="inclusive", user__ui_marker="enumerate")
            else:
                u = sub(f, "groupfilter", function="union", user__ui_domain="database",
                        user__ui_enumeration="inclusive", user__ui_marker="enumerate")
                for v in members:
                    sub(u, "groupfilter", function="member", level=f"[{ref}]", member=v)
        if self.sort:
            sub(view, "sort", class_="computed", column=self.fq(self.sort[0]), direction=self.sort_dir,
                using=self.fq(self.sort[1]))
        if self.filters:
            sl = sub(view, "slices")
            for ref, _, _ in self.filters:
                sub(sl, "column").text = self.fq(ref)
        sub(view, "aggregation", value="true")

        style = sub(table, "style")
        if self.color_map and self.color:
            self.ds.color_maps.setdefault(self.color, {}).update(self.color_map)
        if self.dual:
            sr = sub(style, "style-rule", element="axis")
            sub(sr, "encoding", attr="space", class_="0", field=self.fq(self.dual[0]), field_type="quantitative",
                fold="true", scope="rows", type="space")
        if self.axis_range:
            sr = sub(style, "style-rule", element="axis")
            for ref, kw in self.axis_range.items():
                sub(sr, "encoding", attr="space", class_="0", field=self.fq(ref), field_type="quantitative",
                    scope="rows" if ref in self.rows else "cols", type="space", **kw)
        if self.axis_titles:
            sr = sub(style, "style-rule", element="axis")
            for ref, ttl in self.axis_titles.items():
                sub(sr, "format", attr="title", class_="0", field=self.fq(ref),
                    scope="rows" if ref in self.rows or (self.dual and ref == self.dual[0]) else "cols", value=ttl)
        if self.fmt:
            sr = sub(style, "style-rule", element="cell")
            for ref, f in self.fmt.items():
                sub(sr, "format", attr="text-format", field=self.fq(ref), value=f)
        if len(style) == 0:
            pass

        panes = sub(table, "panes")
        specs = [(None, self.mark, self.mark_color, True)]
        if self.dual:
            specs = [(None, self.mark, self.mark_color, True),
                     (self.rows[-1], self.mark, self.mark_color, True),
                     (self.dual[0], self.dual[1], self.dual[2], False)]
        for idx, (yref, mark, mcolor, main) in enumerate(specs):
            pane = sub(panes, "pane", id=str(idx) if self.dual else None,
                       selection_relaxation_option="selection-relaxation-allow",
                       y_axis_name=self.fq(yref) if yref else None)
            sub(sub(pane, "view"), "breakdown", value="auto")
            sub(pane, "mark", class_=mark)
            enc = sub(pane, "encodings")
            if self.color and main:
                sub(enc, "color", column=self.fq(self.color))
            if self.size and main:
                sub(enc, "size", column=self.fq(self.size))
            if self.text and main:
                sub(enc, "text", column=self.fq(self.text))
            label_refs = re.findall(r"\{([^}]+)\}", "".join(t for t, _ in (self.label_runs or [])))
            for r in label_refs:
                if r != self.text:
                    sub(enc, "text", column=self.fq(r))
            for d in self.detail:
                sub(enc, "lod", column=self.fq(d))
            for t in self.tooltip:
                sub(enc, "tooltip", column=self.fq(t))
            if len(enc) == 0:
                pane.remove(enc)
            if self.label_runs and main:
                runs_xml(sub(pane, "customized-label"), self.label_runs, self.ds.name)
            ps = sub(pane, "style")
            mr = sub(ps, "style-rule", element="mark")
            if self.labels or self.label_runs or (self.text and mark == "Text"):
                sub(mr, "format", attr="mark-labels-show", value="true")
            if mcolor and not (self.color and main):
                sub(mr, "format", attr="mark-color", value=mcolor)
            if len(mr) == 0:
                pane.remove(ps)
        sub(table, "rows").text = self.shelf(list(self.rows) + ([self.dual[0]] if self.dual else []))
        sub(table, "cols").text = self.shelf(list(self.cols))
        return ws


class Dashboard:
    def __init__(self, name, width=1366, height=768, background="#f4f4f2"):
        self.name, self.w, self.h, self.bg = name, width, height, background
        self.items = []

    def sheet(self, name, x, y, w, h, title=True, fit="entire-view"):
        self.items.append(("sheet", name, x, y, w, h, title))
        self.fits = getattr(self, "fits", {})
        self.fits[name] = fit

    def text(self, runs, x, y, w, h, bg=None):
        self.items.append(("text", runs, x, y, w, h, bg))

    def param(self, name, x, y, w, h):
        self.items.append(("param", name, x, y, w, h, None))

    def legend(self, sheet, field, x, y, w, h):
        """Colour legend of a sheet; field = fully qualified field, e.g. [federated.x].[none:line:nk]."""
        self.items.append(("legend", sheet, x, y, w, h, field))

    def xml(self, parent, ids):
        db = sub(parent, "dashboard", name=self.name)
        sr = sub(sub(db, "style"), "style-rule", element="table")
        sub(sr, "format", attr="background-color", value=self.bg)
        sub(db, "size", maxheight=self.h, maxwidth=self.w, minheight=self.h, minwidth=self.w)
        zones = sub(db, "zones")
        root = sub(zones, "zone", h="100000", id=str(next(ids)), type_v2="layout-basic", w="100000", x="0", y="0")
        sub(sub(root, "zone-style"), "format", attr="background-color", value=self.bg)

        def u(px, total):
            return str(round(px * 100000 / total))

        for kind, payload, x, y, w, h, extra in self.items:
            geo = dict(x=u(x, self.w), y=u(y, self.h), w=u(w, self.w), h=u(h, self.h))
            if kind == "sheet":
                z = sub(zones, "zone", h=geo["h"], id=str(next(ids)), name=payload,
                        show_title="true" if extra else "false", w=geo["w"], x=geo["x"], y=geo["y"])
                zs = sub(z, "zone-style")
                for a, v in [("border-color", "#e1e0d9"), ("border-style", "solid"), ("border-width", "1"),
                             ("margin", "4"), ("padding", "6"), ("background-color", "#fcfcfb")]:
                    sub(zs, "format", attr=a, value=v)
            elif kind == "text":
                z = sub(zones, "zone", h=geo["h"], id=str(next(ids)), type_v2="text", w=geo["w"], x=geo["x"], y=geo["y"])
                runs_xml(z, payload)
                zs = sub(z, "zone-style")
                if extra:
                    sub(zs, "format", attr="background-color", value=extra)
                sub(zs, "format", attr="margin", value="2")
            elif kind == "legend":
                z = sub(zones, "zone", h=geo["h"], id=str(next(ids)), name=payload, param=extra, type_v2="color",
                        w=geo["w"], x=geo["x"], y=geo["y"])
                zs = sub(z, "zone-style")
                for a, v in [("border-color", "#e1e0d9"), ("border-style", "solid"), ("border-width", "1"),
                             ("margin", "4"), ("background-color", "#fcfcfb")]:
                    sub(zs, "format", attr=a, value=v)
            elif kind == "param":
                sub(zones, "zone", h=geo["h"], id=str(next(ids)), mode="compact", param=f"[Parameters].[{payload}]",
                    type_v2="paramctrl", w=geo["w"], x=geo["x"], y=geo["y"])
        return db


class Workbook:
    def __init__(self):
        self.datasources, self.sheets, self.dashboards, self.actions = [], [], [], []
        self.params = Params()
        self.active = 0  # dashboard shown when the workbook opens

    def filter_action(self, name, dashboard, source_sheet, exclude=()):
        self.actions.append((name, dashboard, source_sheet, exclude))

    def write(self, path):
        wb = ET.Element("workbook", {"original-version": "18.1", "source-build": "2026.3.0 (20263.26.1022.1645)",
                                     "source-platform": "win", "version": "18.1"})
        sub(wb, "document-format-change-manifest")
        prefs = sub(wb, "preferences")
        sub(prefs, "preference", name="ui.encoding.shelf.height", value="24")
        sub(prefs, "preference", name="ui.shelf.height", value="26")
        cp = sub(prefs, "color-palette", name="Case Study", type="regular")
        for c in ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6", "#898781", "#d03b3b"]:
            sub(cp, "color").text = c
        sub(wb, "style")
        dss = sub(wb, "datasources")
        tmp = ET.Element("tmp")
        for sh in self.sheets:  # first pass: lets sheets register colour maps on their data source
            sh.xml(tmp, self.params)
        if self.params.p:
            self.params.xml(dss)
        for d in self.datasources:
            d.xml(dss)
        if self.actions:
            acts = sub(wb, "actions")
            for i, (name, dash, src, exclude) in enumerate(self.actions, start=1):
                a = sub(acts, "action", caption=name, name=f"[Action{i}]")
                sub(a, "activation", auto_clear="true", type="on-select")
                sub(a, "source", dashboard=dash, type="sheet", worksheet=src)
                cmd = sub(a, "command", command="tsc:tsl-filter")
                if exclude:
                    sub(cmd, "param", name="exclude", value=",".join(exclude))
                sub(cmd, "param", name="special-fields", value="all")
                sub(cmd, "param", name="target", value=dash)
        wss = sub(wb, "worksheets")
        for s in self.sheets:
            s.xml(wss, self.params)
        ids = iter(range(1, 100000))
        if self.dashboards:
            dbs = sub(wb, "dashboards")
            for d in self.dashboards:
                d.xml(dbs, ids)
        win = sub(wb, "windows", source_height="30")
        for s in self.sheets:
            w = sub(win, "window", class_="worksheet", hidden="true", name=s.name)
            cards = sub(w, "cards")
            strip = sub(sub(cards, "edge", name="left"), "strip", size="160")
            for t in ("pages", "filters", "marks"):
                sub(strip, "card", type=t)
            top = sub(cards, "edge", name="top")
            for t in ("columns", "rows", "title"):
                sub(sub(top, "strip", size="2147483647"), "card", type=t)
        for i, d in enumerate(self.dashboards):
            w = sub(win, "window", class_="dashboard", maximized="true" if i == self.active else None, name=d.name)
            vps = sub(w, "viewpoints")
            for kind, payload, *_ in d.items:
                if kind == "sheet":
                    vp = sub(vps, "viewpoint", name=payload)
                    if d.fits.get(payload):
                        sub(vp, "zoom", type=d.fits[payload])
            sub(w, "active", id="-1")
            sub(w, "device-preview")
        ET.indent(wb, space="  ")
        xml = ET.tostring(wb, encoding="unicode")
        xml = xml.replace("<workbook ", "<workbook xmlns:user='http://www.tableausoftware.com/xml/user' ", 1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<?xml version='1.0' encoding='utf-8' ?>\n\n" + xml)
