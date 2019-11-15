from __future__ import absolute_import
from builtins import str
from flask import Blueprint, render_template, current_app, redirect, g, request, session, url_for, Response
from BOFS.globals import db, questionnaires, page_list
from BOFS.util import fetch_condition_count
from .util import sqlalchemy_to_json, verify_admin, escape_csv, questionnaire_name_and_tag
import json
from .questionnaireResults import *
from datetime import datetime
from os import path, listdir
from sqlalchemy.orm.attributes import InstrumentedAttribute


admin = Blueprint('admin', __name__, template_folder='templates', static_folder='static', url_prefix="/admin")


@admin.context_processor
def inject_template_vars():
    """
    Inject additional variables into the context of templates within this blueprint
    See http://flask.pocoo.org/docs/1.0/templating/#context-processors
    :return:
    """

    if "ADDITIONAL_ADMIN_PAGES" in current_app.config:
        additionalAdminPages = current_app.config['ADDITIONAL_ADMIN_PAGES']
    else:
        additionalAdminPages = None

    tableNames = []
    for t in db.metadata.tables:
        tableNames.append(t)

    questionnairesSystem = []

    if path.exists(current_app.root_path + "/questionnaires"):
        for q in listdir(current_app.root_path + "/questionnaires"):
            if q.endswith(".json"):
                questionnairesSystem.append(q.replace(".json", ""))

    tableNames = sorted(tableNames)
    questionnairesLive = current_app.page_list.get_questionnaire_list(True)
    questionnairesLiveUntagged = sorted(current_app.page_list.get_questionnaire_list())
    questionnairesSystem = sorted(questionnairesSystem)

    return dict(
        additionalAdminPages=additionalAdminPages,
        tableNames=tableNames,
        questionnairesLive=questionnairesLive,
        questionnairesLiveUntagged=questionnairesLiveUntagged,
        questionnairesSystem=questionnairesSystem
    )


@admin.route("/")
def admin_index():
    return redirect(url_for("admin.admin_login"))


@admin.route("/login", methods=['GET', 'POST'])
def admin_login():
    if session.get('loggedIn', False):
        return redirect(url_for("admin.route_progress"))

    if request.method == 'POST':
        if request.form['password'] != current_app.config['ADMIN_PASSWORD']:
            return render_template("login_admin.html", message="The password you entered is incorrect.")
        else:
            session['loggedIn'] = True
            session.modified = True

        return redirect(url_for("admin.route_progress"))
    else:
        return render_template("login_admin.html")


def fetch_progress():
    pages = current_app.page_list.flat_page_list()

    progress = db.session.query(db.Participant)

    for page in pages:
        if page['path'] in ["end", "consent"]:  # Don't show end page, use Participant.finished instead.
            pages.remove(page)
            continue

    for page in pages:
        pp = db.aliased(db.Progress, name=page['path'])
        progress = progress.outerjoin(pp, db.and_(
            pp.participantID == db.Participant.participantID,
            pp.path == page['path']
        )).add_entity(
            pp
        )

    progress = progress.all()
    return pages, progress


@admin.route("/progress")
@verify_admin
def route_progress():
    pages, progress = fetch_progress()

    return render_template("progress.html", pages=pages, progress=progress)


@admin.route("/progress_ajax")
@verify_admin
def route_progress_ajax():
    pages, progress = fetch_progress()
    return render_template("progress_ajax.html", pages=pages, progress=progress)



@admin.route("/export_item_timing")
@verify_admin
def route_export_item_timing():
    questionnaires = current_app.page_list.get_questionnaire_list(True)
    header = "participantID,mTurkID"
    output = ""

    headerComplete = False

    results = db.session.query(db.Participant).filter(db.Participant.finished == True).all()

    for p in results:
        output += str.format(u"{},\"{}\"", p.participantID, p.mTurkID.strip())

        for qName in questionnaires:
            tag = ""

            if '/' in qName:
                qNameParts = qName.split('/')
                qName = qNameParts[0]
                tag = qNameParts[1]

            q = p.questionnaire(qName, tag)
            logs = p.questionnaire_log(qName, tag)

            qNameFull = qName
            if len(tag) > 0:
                qNameFull = "{}_{}".format(qName, tag)

            for key in sorted(logs.keys()):
                if not headerComplete:
                    header += ",{}_{}".format(qNameFull, key)

                output += ",{}".format(logs[key])

        output += "\n"
        headerComplete = True

    return render_template("export_csv.html", data=str.format(u"{}\n{}", header, output))


@admin.route("/export")
@admin.route("/export/download", endpoint="route_export_download")
@verify_admin
def route_export():
    includeUnfinished = request.args.get('includeUnfinished', False)

    qList = page_list.get_questionnaire_list(include_tags=True)

    data = db.session.query(db.Participant)

    columns = dict()

    columns['participant'] = [
        "participantID",
        "mTurkID",
        "condition",
        "duration",
        "finished"
    ]

    # First loop constructs the query and fetches the column names
    for qNameAndTag in qList:
        qName, qTag = questionnaire_name_and_tag(qNameAndTag)

        # Add the questionnaire's table/class to the query...
        qDBC = db.aliased(questionnaires[qName].dbClass, name=qNameAndTag)

        data = data.outerjoin(qDBC,
                              db.and_(
                                  qDBC.participantID == db.Participant.participantID,
                                  qDBC.tag == qTag
                              )).add_entity(qDBC)

        attributes = questionnaires[qName].dbClass.__dict__
        keys = sorted(attributes.keys())

        columns[qNameAndTag] = []

        # Make a list of the columns to later construct the CSV header row
        for k in keys:
            if k.startswith("_") \
                    or k.startswith("time") \
                    or k.startswith("participant") \
                    or k == "tag" \
                    or k == str.format(u"{}ID", qName):
                continue
            if not type(attributes[k]) is InstrumentedAttribute:
                continue

            columns[qNameAndTag].append(k)

    if not includeUnfinished:
        data = data.filter(db.Participant.finished == True)

    rows = data.all()

    # Now that the data is loaded, construct the CSV syntax.
    # Starting with the header row...
    column_list = columns['participant']

    for qNameAndTag in qList:
        qName, qTag = questionnaire_name_and_tag(qNameAndTag)

        for col in columns[qNameAndTag]:
            if col.startswith(qName + "_"):  # If it's already prefixed, remove it so the code below works
                col = col.replace(qName, "")

            # Prefix the column with the questionnaire name
            if qTag != "":
                col = qName + "_" + qTag + "_" + col
            else:
                col = qName + "_" + col

            column_list.append(col)

        if qTag != "":
            column_list.append(str.format(u"{}_{}_duration", qName, qTag))
        else:
            column_list.append(str.format(u"{}_duration", qName))

    # Finally construct the CSV string.
    csvString = ",".join(column_list) + "\n"  # CSV Header

    for row in rows:
        csvString += str.format(u"{},{},{},{},{}",
                                row.Participant.participantID,
                                row.Participant.mTurkID,
                                row.Participant.condition,
                                row.Participant.duration,
                                row.Participant.finished
                                )

        for qNameAndTag in qList:
            qData = getattr(row, qNameAndTag)
            for col in columns[qNameAndTag]:
                if qData:
                    csvString += "," + escape_csv(getattr(qData, col))
                else:
                    csvString += ","

            if not qData:  # Special case for duration
                csvString += ","
            else:
                csvString += str.format(u",{}", qData.duration())

        csvString += "\n"

    if request.base_url.endswith("/download"):
        return Response(csvString,
                    mimetype="text/csv",
                    headers={
                        "Content-disposition": "attachment; filename=%s.csv" % ("export_" + datetime.now().strftime("%Y-%m-%d_%H-%M"))
                    })
    else:
        return render_template("export.html", data=csvString)


@admin.route("/preview_questionnaire/<questionnaireName>")
@verify_admin
def route_preview_questionnaire(questionnaireName):
    errors = []

    try:
        f = open(current_app.root_path + '/questionnaires/' + questionnaireName + ".json", 'r')
        jsonData = f.read()
        json.loads(jsonData)
    except Exception as e:
        errors = list(e.args)

    tableName = "questionnaire_" + questionnaireName

    if questionnaireName in page_list.get_questionnaire_list():
        try:
            db.session.query(db.metadata.tables[tableName]).first()
        except Exception as e:
            errors.extend(list(e.args))
            if "(OperationalError) no such column:" in e.args[0]:
                errors.append("Click <a href=\"?fix_errors\">here</a> if you would like to try to automatically add "
                              "this column. Alternatively, you can drop the table and it will be recreated.")
            elif "(OperationalError) no such table:" in e.args[0]:
                errors.append("Click <a href=\"?fix_errors\">here</a> if you would like to try to automatically create "
                              "this table. Alternatively, you can restart the server and it will be created.")

    if 'fix_errors' in request.args:
        # Figure out what column it is by parsing errors.
        for e in errors:
            if "(OperationalError) no such column:" in e:
                e = e.split(tableName + ".")
                columnName = e[len(e)-1]
                dataType = db.metadata.tables[tableName].columns[columnName].type

                addColumn = db.DDL(str.format("ALTER TABLE {} ADD COLUMN {} {}", tableName, columnName, dataType))
                db.engine.execute(addColumn)

                errors.append(str.format(u"{} {} was added to {}. "
                                         u"This error should be gone when you refresh.", columnName, dataType, tableName))

            if "(OperationalError) no such table:" in e:
                db.create_all()
                errors.append(str.format(u"The error should be gone if you refresh."))

    return render_template("preview_questionnaire.html",
                           q=questionnaires[questionnaireName].jsonData,
                           errors=errors)


@admin.route("/analyze_questionnaire/<questionnaireName>/<tag>")
@admin.route("/analyze_questionnaire/<questionnaireName>")
@verify_admin
def route_analyze_questionnaire(questionnaireName, tag=0):
    questionnaire = questionnaires[questionnaireName]

    gridPlotData = {}
    gridPlotJSVars = []

    numericResults = NumericResults(questionnaire.dbClass, questionnaire.fields, tag)

    for condition, valueDict in list(numericResults.dataDescriptive.items()):
        gpd = {
            'name': condition,
            'type': 'bar',
            'x': [field for (field, descriptives) in list(valueDict.items())],
            'y': [descriptives.mean for (field, descriptives) in list(valueDict.items())],
            'error_y': {
                'type': 'data',
                'visible': True,
                'array': [descriptives.sem for (field, descriptives) in list(valueDict.items())]
            }
        }
        gridPlotData[condition] = json.dumps(gpd)
        gridPlotJSVars.append("gpd_{}".format(condition))

    return render_template("questionnaire_results.html",
                           questionnaireName=questionnaireName,
                           tag=tag,
                           conditionCount=fetch_condition_count(),
                           gridPlotData=gridPlotData,
                           gridPlotJSVars=json.dumps(gridPlotJSVars).replace('"', ''),
                           numericResults=numericResults)


def table_data(tableName):
    rows = None
    try:
        rows = db.session.query(db.metadata.tables[tableName]).all()
    except Exception as e:
        return render_template("table_view.html", data="", datafields="", columns="", errors=list(e.args))

    columns = []

    for c in db.metadata.tables[tableName].columns:
        type = str(c.type)
        if type.startswith("VARCHAR") or type.startswith("TEXT"):
            type = u"string"

        column = {'name': c.description, 'type': type.lower()}

        columns.append(column)

    return columns, rows


@admin.route("/table_view/<tableName>")
@verify_admin
def route_table_view(tableName):
    columns, rows = table_data(tableName)
    return render_template("table_view.html", tableName=tableName, rows=rows, columns=columns)


@admin.route("/table_ajax/<tableName>")
@verify_admin
def route_table_ajax(tableName):
    columns, rows = table_data(tableName)
    return render_template("table_ajax.html", rows=rows, columns=columns)


@admin.route("/table_csv/<tableName>")
@verify_admin
def route_table_csv(tableName):
    columns, rows = table_data(tableName)

    csv = ""
    headers = [c['name'] for c in columns]
    csv += ",".join(headers) + "\n"

    for row in rows:
        csv += ",".join([escape_csv(row[i]) for i, c in enumerate(columns)]) + "\n"

    return Response(csv,
                    mimetype="text/csv",
                    headers={
                        "Content-disposition": "attachment; filename=%s.csv" % (tableName + "_" + datetime.now().strftime("%Y-%m-%d"))
                    })