frappe.ui.form.ControlDate = class ControlDate extends frappe.ui.form.ControlData {
	static trigger_change_on_input_event = false;
	make_input() {
		super.make_input();
		this.make_picker();
	}
	make_picker() {
		this.set_date_options();
		this.set_datepicker();
		this.set_t_for_today();
	}
	set_formatted_input(value) {
		if (value === "Today") {
			value = this.get_now_date();
		}

		super.set_formatted_input(value);
		if (this.timepicker_only) return;
		if (!this.datepicker) return;
		if (!value) {
			this.datepicker.clear();
			return;
		}

		let should_refresh = this.last_value && this.last_value !== value;

		if (!should_refresh) {
			if (this.datepicker.selectedDates.length > 0) {
				// if date is selected but different from value, refresh
				const selected_date = moment(this.datepicker.selectedDates[0]).format(
					this.date_format
				);

				should_refresh = selected_date !== value;
			} else {
				// if datepicker has no selected date, refresh
				should_refresh = true;
			}
		}

		if (should_refresh) {
			this.datepicker.selectDate(frappe.datetime.str_to_obj(value));
		}
	}
	set_date_options() {
		// webformTODO:
		let sysdefaults = frappe.boot.sysdefaults;

		let lang = "en";
		frappe.boot.user && (lang = frappe.boot.user.language);
		if (!$.fn.datepicker.language[lang]) {
			lang = "en";
		}

		let date_format =
			sysdefaults && sysdefaults.date_format ? sysdefaults.date_format : "yyyy-mm-dd";

		this.today_text = __("Today");
		this.date_format = frappe.defaultDateFormat;
		this.datepicker_options = {
			language: lang,
			autoClose: true,
			todayButton: true,
			dateFormat: date_format,
			startDate: this.get_start_date(),
			keyboardNav: false,
			minDate: this.df.min_date,
			maxDate: this.df.max_date,
			firstDay: frappe.datetime.get_first_day_of_the_week_index(),
			onSelect: () => {
				this.$input.trigger("change");
			},
			onShow: () => {
				this.datepicker.$datepicker
					.find(".datepicker--button:visible")
					.text(this.today_text);

				this.update_datepicker_position();
			},
			...this.get_df_options(),
		};
	}

	get_start_date() {
		return this.get_now_date();
	}

	set_datepicker() {
		this.$input.datepicker(this.datepicker_options);
		this.datepicker = this.$input.data("datepicker");

		// today button didn't work as expected,
		// so explicitly bind the event
		this.datepicker.$datepicker.find('[data-action="today"]').click(() => {
			this.datepicker.selectDate(this.get_now_date());
		});
	}
	update_datepicker_position() {
		// show datepicker above or below the input
		// based on scroll position
		const picker_width = this.datepicker.$datepicker.outerWidth() + 25;
		const picker_height = this.datepicker.$datepicker.outerHeight() + 25;

		const input_offset = this.$input.offset();
		const input_height = this.$input.outerHeight();

		const window_height = $(window).height();
		const window_width = $(window).width();
		const scroll_top = $(window).scrollTop();
		const scroll_left = $(window).scrollLeft();

		const bottom_space = window_height - (input_offset.top + input_height - scroll_top);
		const right_space = window_width - (input_offset.left - scroll_left);

		let position;
		if (bottom_space < picker_height) {
			if (right_space < picker_width) {
				position = "top right";
			} else {
				position = "top left";
			}
		} else {
			if (right_space < picker_width) {
				position = "bottom right";
			} else {
				position = "bottom left";
			}
		}

		this.datepicker.update("position", position, true);
	}
	get_now_date() {
		return frappe.datetime
			.convert_to_system_tz(frappe.datetime.now_date(true), false)
			.toDate();
	}
	set_t_for_today() {
		var me = this;
		this.$input.on("keydown", function (e) {
			if (e.which === 84) {
				// 84 === t
				if (me.df.fieldtype == "Date") {
					me.set_value(frappe.datetime.nowdate());
				}
				if (me.df.fieldtype == "Datetime") {
					me.set_value(frappe.datetime.now_datetime());
				}
				if (me.df.fieldtype == "Time") {
					me.set_value(frappe.datetime.now_time());
				}
				return false;
			}
		});
	}
	parse(value) {
		if (value) {
			if (value == "Invalid date") {
				return "";
			}
			return frappe.datetime.user_to_str(value, false, true);
		}
	}
	format_for_input(value) {
		if (value) {
			return frappe.datetime.str_to_user(value, false, true);
		}
		return "";
	}
	validate(value) {
		if (value && !frappe.datetime.validate(value)) {
			let sysdefaults = frappe.sys_defaults;
			let date_format =
				sysdefaults && sysdefaults.date_format ? sysdefaults.date_format : "yyyy-mm-dd";
			frappe.msgprint(__("Date {0} must be in format: {1}", [value, date_format]));
			return "";
		}
		return value;
	}
	get_df_options() {
		let df_options = this.df.options;
		if (!df_options) return {};

		let options = {};
		if (typeof df_options === "string") {
			try {
				options = JSON.parse(df_options);
			} catch (error) {
				console.warn(`Invalid JSON in options of "${this.df.fieldname}"`);
			}
		} else if (typeof df_options === "object") {
			options = df_options;
		}
		return options;
	}
};
