# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.model.document import Document
from frappe.utils.jinja import validate_template


class AutoValueSetter(Document):
	def validate(self):
		self.validate_doctype()
		self.validate_conditions()
		frappe.cache().hdel('auto_value_setters', self.document_type)

	def on_change(self):
		frappe.cache().hdel('auto_value_setters', self.document_type)

	def validate_doctype(self):
		prohibited_doctypes = [self.doctype, 'DocType', 'DocField']
		prohibited_doctypes += [d.options for d in self.meta.get_table_fields()]

		if self.document_type in prohibited_doctypes:
			frappe.throw(_("Cannot set Auto Value Setter for itself"))

		meta = frappe.get_meta(self.document_type)
		if not meta:
			frappe.throw(_("Could not find Document Type {0}").format(self.document_type))

		if not meta.get("allow_auto_value_setter"):
			frappe.throw(_("Auto Value Setter not allowed for Document Type {0}. Please enable it using Customize Form").format(self.document_type))

		if not meta.get_field(self.field_name):
			frappe.throw(_("Field Name {0} does not exist in Document Type {1}".format(self.field_name, self.document_type)))

		if meta.issingle:
			frappe.throw(_("Cannot set Auto Value Setter for Single Document Types"))

	def validate_conditions(self):
		for d in self.conditions:
			validate_template(cstr(d.value))


def apply_auto_value_setters(doc, parent=None):
	names = frappe.cache().hget('auto_value_setters', doc.doctype)
	if names is None:
		names = [d.name for d in frappe.get_all('Auto Value Setter', filters={'enabled': 1, 'document_type': doc.doctype})]
		frappe.cache().hset('auto_value_setters', doc.doctype, names)

	is_submitted = doc.meta.is_submittable and doc.docstatus == 1
	context = get_context(doc, parent)

	for name in names:
		auto_value_setter = frappe.get_cached_doc("Auto Value Setter", name)

		df = doc.meta.get_field(auto_value_setter.field_name)
		current_value = doc.get(auto_value_setter.field_name)

		if not df:
			continue
		if auto_value_setter.set_for_new_document and not doc.get("__islocal"):
			continue
		if auto_value_setter.set_if_empty and current_value:
			continue
		if is_submitted and not df.allow_on_submit:
			continue
		# if not doc.get("__islocal") and df.set_only_once and doc.get("_doc_before_save", {}).get(auto_value_setter.field_name):
		# 	continue

		for d in auto_value_setter.conditions:
			if not d.condition or frappe.safe_eval(d.condition, None, context):  # if condition is met
				value = frappe.render_template(cstr(d.value), context)
				doc.set(auto_value_setter.field_name, value)
				break

def get_context(doc, parent):
	return {"doc": doc, "parent": parent, "nowdate": frappe.utils.nowdate, "frappe.utils": frappe.utils,
		"frappe": frappe}
