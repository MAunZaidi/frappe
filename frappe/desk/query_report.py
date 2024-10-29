# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import datetime
import json
import os
from datetime import timedelta

import frappe
import frappe.desk.reportview
from frappe import _
from frappe.core.utils import ljust_list
from frappe.desk.reportview import clean_params, parse_json
from frappe.model.utils import render_include
from frappe.modules import get_module_path, scrub
from frappe.monitor import add_data_to_monitor
from frappe.permissions import get_role_permissions
from collections import OrderedDict
from frappe.utils import cint, cstr, flt, format_duration, get_html_format, sbool


def get_report_doc(report_name):
	doc = frappe.get_doc("Report", report_name)
	doc.custom_columns = []
	doc.custom_filters = []

	if doc.report_type == "Custom Report":
		custom_report_doc = doc
		doc = get_reference_report(doc)
		doc.custom_report = report_name
		if custom_report_doc.json:
			data = json.loads(custom_report_doc.json)
			if data:
				doc.custom_columns = data.get("columns")
				doc.custom_filters = data.get("filters")
		doc.is_custom_report = True

		# Follow whatever the custom report has set for prepared report field
		doc.prepared_report = custom_report_doc.prepared_report

	if not doc.is_permitted():
		frappe.throw(
			_("You don't have access to Report: {0}").format(report_name),
			frappe.PermissionError,
		)

	if not frappe.has_permission(doc.ref_doctype, "report"):
		frappe.throw(
			_("You don't have permission to get a report on: {0}").format(doc.ref_doctype),
			frappe.PermissionError,
		)

	if doc.disabled:
		frappe.throw(_("Report {0} is disabled").format(report_name))

	return doc


def get_report_result(report, filters):
	res = None

	if report.report_type == "Query Report":
		res = report.execute_query_report(filters)

	elif report.report_type == "Script Report":
		res = report.execute_script_report(filters)

	elif report.report_type == "Custom Report":
		ref_report = get_report_doc(report.report_name)
		res = get_report_result(ref_report, filters)

	return res


@frappe.read_only()
def generate_report_result(
	report, filters=None, user=None, custom_columns=None, is_tree=False, parent_field=None
):
	user = user or frappe.session.user
	filters = filters or []

	if filters and isinstance(filters, str):
		filters = json.loads(filters)

	res = get_report_result(report, filters) or []

	columns, result, message, chart, report_summary, skip_total_row = ljust_list(res, 6)
	columns = [get_column_as_dict(col) for col in (columns or [])]
	report_column_names = [col["fieldname"] for col in columns]
	# convert to list of dicts

	result = normalize_result(result, columns)

	if report.custom_columns:
		# saved columns (with custom columns / with different column order)
		columns = report.custom_columns

	# unsaved custom_columns
	if custom_columns:
		for custom_column in custom_columns:
			columns.insert(custom_column["insert_after_index"] + 1, custom_column)

	# all columns which are not in original report
	report_custom_columns = [column for column in columns if column["fieldname"] not in report_column_names]

	if report_custom_columns:
		result = add_custom_column_data(report_custom_columns, result)

	if result:
		result = get_filtered_data(report.ref_doctype, columns, result, user)

	if cint(report.add_total_row) and result and not skip_total_row:
		result = add_total_row(result, columns, is_tree=is_tree, parent_field=parent_field)

	return {
		"result": result,
		"columns": columns,
		"message": message,
		"chart": chart,
		"report_summary": report_summary,
		"skip_total_row": skip_total_row or 0,
		"status": None,
		"execution_time": frappe.cache.hget("report_execution_time", report.name) or 0,
	}


def normalize_result(result, columns):
	# Converts to list of dicts from list of lists/tuples
	data = []
	column_names = [column["fieldname"] for column in columns]
	if result and isinstance(result[0], list | tuple):
		for row in result:
			row_obj = {}
			for idx, column_name in enumerate(column_names):
				row_obj[column_name] = row[idx]
			data.append(row_obj)
	else:
		data = result

	return data


@frappe.whitelist()
def get_script(report_name):
	report = get_report_doc(report_name)
	module = report.module or frappe.db.get_value("DocType", report.ref_doctype, "module")

	is_custom_module = frappe.get_cached_value("Module Def", module, "custom")

	# custom modules are virtual modules those exists in DB but not in disk.
	module_path = "" if is_custom_module else get_module_path(module)
	report_folder = module_path and os.path.join(module_path, "report", scrub(report.name))
	script_path = report_folder and os.path.join(report_folder, scrub(report.name) + ".js")
	print_path = report_folder and os.path.join(report_folder, scrub(report.name) + ".html")

	script = None
	if os.path.exists(script_path):
		with open(script_path) as f:
			script = f.read()
			script += f"\n\n//# sourceURL={scrub(report.name)}.js"

	html_format = get_html_format(print_path)

	if not script and report.javascript:
		script = report.javascript
		script += f"\n\n//# sourceURL={scrub(report.name)}__custom"

	if not script:
		script = "frappe.query_reports['%s']={}" % report_name

	return {
		"script": render_include(script),
		"html_format": html_format,
		"execution_time": frappe.cache.hget("report_execution_time", report_name) or 0,
		"filters": report.filters,
		"custom_report_name": report.name if report.get("is_custom_report") else None,
	}


def get_reference_report(report):
	if report.report_type != "Custom Report":
		return report
	reference_report = frappe.get_doc("Report", report.reference_report)
	return get_reference_report(reference_report)


@frappe.whitelist()
@frappe.read_only()
def run(
	report_name,
	filters=None,
	user=None,
	ignore_prepared_report=False,
	custom_columns=None,
	is_tree=False,
	parent_field=None,
	are_default_filters=True,
):
	report = get_report_doc(report_name)
	if not user:
		user = frappe.session.user
	if not frappe.has_permission(report.ref_doctype, "report"):
		frappe.msgprint(
			_("Must have report permission to access this report."),
			raise_exception=True,
		)

	result = None

	if sbool(are_default_filters) and report.custom_filters:
		filters = report.custom_filters

	try:
		if report.prepared_report and not sbool(ignore_prepared_report) and not custom_columns:
			if filters:
				if isinstance(filters, str):
					filters = json.loads(filters)

				dn = filters.pop("prepared_report_name", None)
			else:
				dn = ""
			result = get_prepared_report_result(report, filters, dn, user)
		else:
			result = generate_report_result(report, filters, user, custom_columns, is_tree, parent_field)
			add_data_to_monitor(report=report.reference_report or report.name)
	except Exception:
		frappe.log_error("Report Error")
		raise

	result["add_total_row"] = report.add_total_row and not result.get("skip_total_row", False)

	if sbool(are_default_filters) and report.custom_filters:
		result["custom_filters"] = report.custom_filters

	return result


def add_custom_column_data(custom_columns, result):
	doctype_names_from_custom_field = []
	for column in custom_columns:
		if len(column["fieldname"].split("-")) > 1:
			# length greater than 1, means that the column is a custom field with confilicting fieldname
			doctype_name = frappe.unscrub(column["fieldname"].split("-")[1])
			doctype_names_from_custom_field.append(doctype_name)
		column["fieldname"] = column["fieldname"].split("-")[0]

	custom_column_data = get_data_for_custom_report(custom_columns, result)

	for column in custom_columns:
		key = (column.get("doctype"), column.get("fieldname"))
		if key in custom_column_data:
			for row in result:
				link_field = column.get("link_field")

				# backwards compatibile `link_field`
				# old custom reports which use `str` should not break.
				if isinstance(link_field, str):
					link_field = frappe._dict({"fieldname": link_field, "names": []})

				row_reference = row.get(link_field.get("fieldname"))
				# possible if the row is empty
				if not row_reference:
					continue
				if key[0] in doctype_names_from_custom_field:
					column["fieldname"] = column.get("id")
				row[column.get("fieldname")] = custom_column_data.get(key).get(row_reference)

	return result


def get_prepared_report_result(report, filters, dn="", user=None):
	from frappe.core.doctype.prepared_report.prepared_report import get_completed_prepared_report

	def get_report_data(doc, data):
		# backwards compatibility - prepared report used to have a columns field,
		# we now directly fetch it from the result file
		if doc.get("columns") or isinstance(data, list):
			columns = (doc.get("columns") and json.loads(doc.columns)) or data[0]
			data = {"result": data}
		else:
			columns = data.get("columns")

		for column in columns:
			if isinstance(column, dict) and column.get("label"):
				column["label"] = _(column["label"])

		return data | {"columns": columns}

	report_data = {}
	if not dn:
		dn = get_completed_prepared_report(
			filters, user, report.get("custom_report") or report.get("report_name")
		)

	doc = frappe.get_doc("Prepared Report", dn) if dn else None
	if doc:
		try:
			if data := json.loads(doc.get_prepared_data().decode("utf-8")):
				report_data = get_report_data(doc, data)
		except Exception as e:
			doc.log_error("Prepared report render failed")
			frappe.msgprint(_("Prepared report render failed") + f": {e!s}")
			doc = None

	return report_data | {"prepared_report": True, "doc": doc}


@frappe.whitelist()
def export_query():
	"""export from query reports"""
	from frappe.desk.utils import get_csv_bytes, pop_csv_params, provide_binary_file

	form_params = frappe._dict(frappe.local.form_dict)
	csv_params = pop_csv_params(form_params)
	clean_params(form_params)
	parse_json(form_params)

	report_name = form_params.report_name
	frappe.permissions.can_export(
		frappe.get_cached_value("Report", report_name, "ref_doctype"),
		raise_exception=True,
	)

	file_format_type = form_params.file_format_type or "Excel"
	custom_columns = frappe.parse_json(form_params.custom_columns or "[]")
	include_indentation = cint(form_params.include_indentation)
	include_filters = cint(form_params.include_filters)
	filters = frappe.parse_json(form_params.filters or "{}")
	visible_idx = frappe.parse_json(form_params.visible_idx or "[]")

	columns = frappe.parse_json(form_params.columns)
	if not columns:
		frappe.respond_as_web_page(
			_("No data to export"),
			_("You can try changing the filters of your report."),
		)
		return

	report_data = frappe.parse_json(form_params.data)
	format_duration_fields(report_data, columns)

	xlsx_data, column_widths, column_formats = build_xlsx_data(
		{"columns": columns, "result": report_data, "filters": filters},
		ignore_visible_idx=True,
		visible_idx=[],
		include_indentation=include_indentation,
		include_filters=include_filters,
	)

	if file_format_type == "CSV":
		content = get_csv_bytes(xlsx_data, csv_params)
		file_extension = "csv"
	elif file_format_type == "Excel":
		from frappe.utils.xlsxutils import make_xlsx
		file_extension = "xlsx"
		content = make_xlsx(xlsx_data, "Query Report",
			column_widths=column_widths, column_formats=column_formats, freeze="A2")

	provide_binary_file(report_name, file_extension, content)

def format_duration_fields(data: list, columns: list) -> None:
	for i, col in enumerate(columns):
		if col.get("fieldtype") != "Duration":
			continue

		for row in data:
			index = col.get("fieldname") if isinstance(row, dict) else i
			if row[index]:
				row[index] = format_duration(row[index])


def build_xlsx_data(data, visible_idx, include_indentation, include_filters=False, ignore_visible_idx=False):
	EXCEL_TYPES = (
		str,
		bool,
		type(None),
		int,
		float,
		datetime.datetime,
		datetime.date,
		datetime.time,
		datetime.timedelta,
	)

	if len(visible_idx) == len(data["result"]):
		# It's not possible to have same length and different content.
		ignore_visible_idx = True
	else:
		# Note: converted for faster lookups
		visible_idx = set(visible_idx)

	result = []
	column_widths = []
	column_formats = []

	if cint(include_filters):
		filter_data = []
		filters = data.get("filters") or {}
		for filter_name, filter_value in filters.items():
			if not filter_value:
				continue
			filter_value = (
				", ".join([cstr(x) for x in filter_value])
				if isinstance(filter_value, list)
				else cstr(filter_value)
			)
			filter_data.append([cstr(filter_name), filter_value])
		filter_data.append([])
		result += filter_data

	column_data = []
	for column in data["columns"]:
		if column.get("hidden"):
			continue
		column_data.append(_(column.get("label")))
		column_width = cint(column.get("width", 0))
		# to convert into scale accepted by openpyxl
		column_width /= 8
		column_widths.append(column_width)

		column_format = "General"
		fieldtype = column.get("fieldtype")
		if fieldtype in ("Currency", "Float", "Percent"):
			from frappe.model.meta import get_field_precision
			number_format = frappe.db.get_default("number_format") or "#,###.##"
			decimal_str, comma_str, precision = frappe.utils.get_number_format_info(number_format)
			precision = 1 if fieldtype == "Percent" else get_field_precision(column)
			column_format = f"#{comma_str}##0{decimal_str}{'0' * cint(precision)}"
			if fieldtype == "Percent":
				column_format += "\\%"

		column_formats.append(column_format)

	result.append(column_data)

	# build table from result
	for row_idx, row in enumerate(data["result"]):
		# only pick up rows that are visible in the report
		if ignore_visible_idx or row_idx in visible_idx:
			row_data = []
			if isinstance(row, dict):
				for col_idx, column in enumerate(data["columns"]):
					if column.get("hidden"):
						continue
					label = column.get("label")
					fieldname = column.get("fieldname")
					cell_value = row.get(fieldname, row.get(label, ""))
					if not isinstance(cell_value, EXCEL_TYPES):
						cell_value = cstr(cell_value)

					if cint(include_indentation) and "indent" in row and col_idx == 0:
						cell_value = ("    " * cint(row["indent"])) + cstr(cell_value)
					row_data.append(cell_value)
			elif row:
				row_data = row

			result.append(row_data)

	return result, column_widths, column_formats


def add_total_row(result, columns, meta=None, is_tree=False, parent_field=None):
	total_row = [""] * len(columns)
	has_percent = []

	for i, col in enumerate(columns):
		fieldtype, options, fieldname = None, None, None
		if isinstance(col, str):
			if meta:
				# get fieldtype from the meta
				field = meta.get_field(col)
				if field:
					fieldtype = meta.get_field(col).fieldtype
					fieldname = meta.get_field(col).fieldname
			else:
				col = col.split(":")
				if len(col) > 1:
					if col[1]:
						fieldtype = col[1]
						if "/" in fieldtype:
							fieldtype, options = fieldtype.split("/")
					else:
						fieldtype = "Data"
		else:
			fieldtype = col.get("fieldtype")
			fieldname = col.get("fieldname")
			options = col.get("options")

		for row in result:
			if i >= len(row):
				continue

			if isinstance(row, dict) and row.get("_excludeFromTotal"):
				continue

			cell = row.get(fieldname) if isinstance(row, dict) else row[i]
			if fieldtype is None:
				if isinstance(cell, int):
					fieldtype = "Int"
				elif isinstance(cell, float):
					fieldtype = "Float"
			if fieldtype in ["Currency", "Int", "Float", "Percent", "Duration"] and flt(cell):
				if not (is_tree and row.get(parent_field)):
					total_row[i] = flt(total_row[i]) + flt(cell)

			if fieldtype == "Percent" and i not in has_percent:
				has_percent.append(i)

			if fieldtype == "Time" and cell:
				if not total_row[i]:
					total_row[i] = timedelta(hours=0, minutes=0, seconds=0)
				total_row[i] = total_row[i] + cell

		if fieldtype == "Link" and options == "Currency":
			total_row[i] = result[0].get(fieldname) if isinstance(result[0], dict) else result[0][i]

	for i in has_percent:
		total_row[i] = flt(total_row[i]) / len(result)

	first_col_fieldtype = None
	if isinstance(columns[0], str):
		first_col = columns[0].split(":")
		if len(first_col) > 1:
			first_col_fieldtype = first_col[1].split("/", 1)[0]
	else:
		first_col_fieldtype = columns[0].get("fieldtype")

	if first_col_fieldtype not in ["Currency", "Int", "Float", "Percent", "Date"]:
		total_row[0] = _("Total")

	result.append(total_row)
	return result


@frappe.whitelist()
def get_data_for_custom_field(doctype, field, names=None):
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(_("Not Permitted to read {0}").format(doctype), frappe.PermissionError)

	filters = {}
	if names:
		if isinstance(names, str | bytearray):
			names = frappe.json.loads(names)
		filters.update({"name": ["in", names]})

	return frappe._dict(frappe.get_list(doctype, filters=filters, fields=["name", field], as_list=1))


def get_data_for_custom_report(columns, result):
	doc_field_value_map = {}

	for column in columns:
		if link_field := column.get("link_field"):
			# backwards compatibile `link_field`
			# old custom reports which use `str` should not break
			if isinstance(link_field, str):
				link_field = frappe._dict({"fieldname": link_field, "names": []})

			fieldname = column.get("fieldname")
			doctype = column.get("doctype")

			row_key = link_field.get("fieldname")
			names = []
			for row in result:
				if row.get(row_key):
					names.append(row.get(row_key))
			names = list(set(names))

			doc_field_value_map[(doctype, fieldname)] = get_data_for_custom_field(doctype, fieldname, names)
	return doc_field_value_map


@frappe.whitelist()
def save_report(reference_report, report_name, columns, filters):
	report_doc = get_report_doc(reference_report)

	docname = frappe.db.exists(
		"Report",
		{
			"report_name": report_name,
			"is_standard": "No",
			"report_type": "Custom Report",
		},
	)

	if docname:
		report = frappe.get_doc("Report", docname)
		existing_jd = json.loads(report.json)
		existing_jd["columns"] = json.loads(columns)
		existing_jd["filters"] = json.loads(filters)
		report.update({"json": json.dumps(existing_jd, separators=(",", ":"))})
		report.save()
		frappe.msgprint(_("Report updated successfully"))

		return docname
	else:
		new_report = frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": report_name,
				"json": f'{{"columns":{columns},"filters":{filters}}}',
				"ref_doctype": report_doc.ref_doctype,
				"is_standard": "No",
				"report_type": "Custom Report",
				"reference_report": reference_report,
			}
		).insert(ignore_permissions=True)
		frappe.msgprint(_("{0} saved successfully").format(new_report.name))
		return new_report.name


def get_filtered_data(ref_doctype, columns, data, user):
	result = []
	linked_doctypes = get_linked_doctypes(columns, data)
	match_filters_per_doctype = get_user_match_filters(linked_doctypes, user=user)
	shared = frappe.share.get_shared(ref_doctype, user)
	columns_dict = get_columns_dict(columns)

	role_permissions = get_role_permissions(frappe.get_meta(ref_doctype), user)
	if_owner = role_permissions.get("if_owner", {}).get("report")

	if match_filters_per_doctype:
		for row in data:
			# Why linked_doctypes.get(ref_doctype)? because if column is empty, linked_doctypes[ref_doctype] is removed
			if (
				linked_doctypes.get(ref_doctype)
				and shared
				and row.get(linked_doctypes[ref_doctype]) in shared
			):
				result.append(row)

			elif has_match(
				row,
				linked_doctypes,
				match_filters_per_doctype,
				ref_doctype,
				if_owner,
				columns_dict,
				user,
			):
				result.append(row)
	else:
		result = list(data)

	return result


def has_match(
	row,
	linked_doctypes,
	doctype_match_filters,
	ref_doctype,
	if_owner,
	columns_dict,
	user,
):
	"""Returns True if after evaluating permissions for each linked doctype
	- There is an owner match for the ref_doctype
	- `and` There is a user permission match for all linked doctypes

	Returns True if the row is empty

	Note:
	Each doctype could have multiple conflicting user permission doctypes.
	Hence even if one of the sets allows a match, it is true.
	This behavior is equivalent to the trickling of user permissions of linked doctypes to the ref doctype.
	"""
	resultant_match = True

	if not row:
		# allow empty rows :)
		return resultant_match

	for doctype, filter_list in doctype_match_filters.items():
		matched_for_doctype = False

		if doctype == ref_doctype and if_owner:
			idx = linked_doctypes.get("User")
			if idx is not None and row[idx] == user and columns_dict[idx] == columns_dict.get("owner"):
				# owner match is true
				matched_for_doctype = True

		if not matched_for_doctype:
			for match_filters in filter_list:
				match = True
				for dt, idx in linked_doctypes.items():
					# case handled above
					if dt == "User" and columns_dict[idx] == columns_dict.get("owner"):
						continue
					if columns_dict[idx].get("ignore_user_permissions"):
						continue

					cell_value = None
					if isinstance(row, dict):
						cell_value = row.get(idx)
					elif isinstance(row, list | tuple):
						cell_value = row[idx]

					if (
						dt in match_filters
						and cell_value not in match_filters.get(dt)
						and frappe.db.exists(dt, cell_value)
					):
						match = False
						break

				# each doctype could have multiple conflicting user permission doctypes, hence using OR
				# so that even if one of the sets allows a match, it is true
				matched_for_doctype = matched_for_doctype or match

				if matched_for_doctype:
					break

		# each doctype's user permissions should match the row! hence using AND
		resultant_match = resultant_match and matched_for_doctype

		if not resultant_match:
			break

	return resultant_match


def get_linked_doctypes(columns, data):
	linked_doctypes = {}

	columns_dict = get_columns_dict(columns)

	for idx in range(len(columns)):
		df = columns_dict[idx]
		if df.get("fieldtype") == "Link":
			if data and isinstance(data[0], list | tuple):
				linked_doctypes[df["options"]] = idx
			else:
				# dict
				linked_doctypes[df["options"]] = df["fieldname"]

	# remove doctype if column is empty
	columns_with_value = []
	for row in data:
		if row:
			if len(row) != len(columns_with_value):
				if isinstance(row, list | tuple):
					row = enumerate(row)
				elif isinstance(row, dict):
					row = row.items()

				for col, val in row:
					if val and col not in columns_with_value:
						columns_with_value.append(col)

	items = list(linked_doctypes.items())

	for doctype, key in items:
		if key not in columns_with_value:
			del linked_doctypes[doctype]

	return linked_doctypes


def get_columns_dict(columns):
	"""Returns a dict with column docfield values as dict
	The keys for the dict are both idx and fieldname,
	so either index or fieldname can be used to search for a column's docfield properties
	"""
	columns_dict = frappe._dict()
	for idx, col in enumerate(columns):
		col_dict = get_column_as_dict(col)
		columns_dict[idx] = col_dict
		columns_dict[col_dict["fieldname"]] = col_dict

	return columns_dict


def get_column_as_dict(col):
	col_dict = frappe._dict()

	# string
	if isinstance(col, str):
		col = col.split(":")
		if len(col) > 1:
			if "/" in col[1]:
				col_dict["fieldtype"], col_dict["options"] = col[1].split("/")
			else:
				col_dict["fieldtype"] = col[1]
			if len(col) == 3:
				col_dict["width"] = col[2]

		col_dict["label"] = col[0]
		col_dict["fieldname"] = frappe.scrub(col[0])

	# dict
	else:
		col_dict.update(col)
		if "fieldname" not in col_dict:
			col_dict["fieldname"] = frappe.scrub(col_dict["label"])

	return col_dict


def get_user_match_filters(doctypes, user):
	match_filters = {}

	for dt in doctypes:
		filter_list = frappe.desk.reportview.build_match_conditions(dt, user, False)
		if filter_list:
			match_filters[dt] = filter_list

	return match_filters


def group_report_data(
	rows_to_group, group_by, group_by_labels=None,
	total_fields=None, totals_only=False,
	calculate_totals=None, postprocess_group=None, starting_level=1,
):
	return _group_report_data(
		rows_to_group=rows_to_group,
		group_by=group_by,
		group_by_labels=group_by_labels,
		total_fields=total_fields,
		totals_only=totals_only,
		calculate_totals=calculate_totals,
		postprocess_group=postprocess_group,
		level=cint(starting_level)
	)


def _group_report_data(
	rows_to_group, group_by, group_by_labels=None,
	total_fields=None, totals_only=False,
	calculate_totals=None, postprocess_group=None,
	parent_grouped_by=None, level=1, level_idx=None
):
	def get_grouped_by_map(group):
		res = parent_grouped_by.copy()
		if isinstance(group_field, (list, tuple)):
			for i, f in enumerate(group_field):
				res[f] = group[i]
		else:
			res[group_field] = group
		return res

	def set_group_idx(_rows):
		for i, d in enumerate(_rows):
			level_idx[level] = i + 1
			d["_level_idx"] = level_idx[level]
			d["_group_idx"] = get_group_idx()

	def get_group_idx():
		if level < 1:
			return ""

		group_idx = []
		for l in range(level):
			group_idx.append(str(level_idx[l+1]))

		group_idx_str = ".".join(group_idx)
		return group_idx_str

	# Initialize level
	level = cint(level)
	if not level_idx:
		level_idx = {}

	level_idx[level] = 0

	# Break condition
	if not group_by and group_by is not None:
		set_group_idx(rows_to_group)
		return rows_to_group

	# Intialize grouping
	if not isinstance(group_by, list):
		group_by = [group_by]
	if not group_by_labels:
		group_by_labels = {}
	if not parent_grouped_by:
		parent_grouped_by = OrderedDict()

	group_field = group_by[0] or ''
	group_label = group_by_labels.get(group_field) if group_by_labels.get(group_field) else frappe.unscrub(cstr(group_field))
	group_rows = OrderedDict()
	group_totals = OrderedDict()

	# Create group dictionaries
	for row in rows_to_group:
		if not group_field:
			group_value = ''
		elif isinstance(group_field, (list, tuple)):
			group_value = tuple(map(lambda f: row.get(f), group_field))
		else:
			group_value = row.get(group_field)

		group_rows.setdefault(group_value, []).append(row)

		# Calculate totals if total fields provided
		if total_fields:
			group_totals.setdefault(group_value, {})
			for total_field in total_fields:
				group_totals[group_value].setdefault(total_field, 0)
				group_totals[group_value][total_field] += flt(row.get(total_field))

	# Call User Provided calculate_totals
	if calculate_totals and callable(calculate_totals):
		for group_value in group_rows.keys():
			grouped_by_map = get_grouped_by_map(group_value)
			group_totals[group_value] = calculate_totals(group_rows[group_value], group_field, group_value, grouped_by_map)

	# Group totals only break condition
	if totals_only and len(group_by) == 1:
		out = list(group_totals.values())
		set_group_idx(out)
		return out

	out = []

	# Create group objects and recurse
	for group_value, rows in group_rows.items():
		level_idx[level] += 1

		grouped_by_map = get_grouped_by_map(group_value)
		group_object = frappe._dict({
			"_isGroup": 1,
			"_level_idx": level_idx[level],
			"_group_idx": get_group_idx(),
			"group_field": group_field,
			"group_label": group_label,
			"group_value": group_value,
			"rows": _group_report_data(
				rows_to_group=rows,
				group_by=group_by[1:],
				group_by_labels=group_by_labels,
				totals_only=totals_only,
				total_fields=total_fields,
				calculate_totals=calculate_totals,
				postprocess_group=postprocess_group,
				parent_grouped_by=grouped_by_map,
				level=level+1,
				level_idx=level_idx,
			),
		})

		for f, g in grouped_by_map.items():
			if f not in group_object and f is not None:
				group_object[f] = g

		if group_totals.get(group_value):
			group_total_row = group_totals.get(group_value)
			group_total_row['_bold'] = 1
			group_total_row['_group_idx'] = group_object['_group_idx']
			group_total_row['_level_idx'] = group_object['_level_idx']
			group_object['totals'] = group_total_row

		if postprocess_group and callable(postprocess_group):
			postprocess_group(group_object, grouped_by_map)

		if group_object.totals:
			group_object.totals['_isGroupTotal'] = 1

		if group_object.rows or group_object.totals:
			out.append(group_object)

	return out


def flatten_grouped_report_data(data, result=None, parent_indent=None):
	result = result or []

	for obj in data:
		if isinstance(obj, dict) and obj.get("_isGroup"):
			if obj.get("totals"):
				obj["totals"]["indent"] = obj["totals"].get("indent") or 0
				parent_indent = obj["totals"]["indent"]

				result.append(obj.get("totals"))

			if obj.get("rows"):
				flatten_grouped_report_data(obj.get("rows"), result=result, parent_indent=parent_indent)
		else:
			if parent_indent is not None:
				obj["indent"] = parent_indent + 1

			result.append(obj)

	return result


def hide_columns_if_filtered(columns, filters):
	def condition(col):
		if col.get('hide_if_filtered'):
			filter_value = filters.get(col.get('filter_fieldname') or col['fieldname'])
			if filter_value:
				if isinstance(filter_value, list):
					return len(filter_value) == 1
				else:
					return True
		return False

	return [c for c in columns if not condition(c)]
