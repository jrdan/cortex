#!/usr/bin/python
#

from cortex import app
import cortex.lib.core
import cortex.lib.systems
import cortex.lib.cmdb
import cortex.lib.classes
from cortex.neocortex import NeoCortexLib
from flask import Flask, request, session, redirect, url_for, flash, g, abort, make_response, render_template, jsonify, Response
import os 
import time
import json
import re
import werkzeug
import MySQLdb as mysql
import yaml
import csv
import io
import requests

################################################################################

@app.route('/systems')
@cortex.lib.user.login_required
def systems():
	"""Shows the list of known systems to the user."""

	# Get the list of systems
	systems = cortex.lib.systems.get_systems()


	# Get the list of active classes (used to populate the tab bar)
	classes = cortex.lib.classes.list()

	# Get the search string, if any
	q = request.args.get('q', None)
	
	# Strip any leading and or trailing spaces
	if q is not None:
		q = q.strip()

	# Render
	return render_template('systems.html', systems=systems, classes=classes, active='systems', title="Systems", q=q)

################################################################################

## TODO: this function is not used anymore? Check and remove
@app.route('/systems/search')
@cortex.lib.user.login_required
def systems_search():
	"""Allows the user to search for a system by entering its name in the 
	search box"""

	# Get the query from the URL
	query = request.args.get('query')
	if query is None:
		app.logger.warn('Missing \'query\' parameter in systems search request')
		return abort(400)

	# Search for the system
	system = cortex.lib.systems.get_system_by_name(query)

	if system is not None:
		# If we found the system, redirect to the system's edit page
		return redirect(url_for('systems_edit', id=system['id']))
	else:
		# If we didn't find the system, search for it instead
		return redirect(url_for('systems',q=query))

################################################################################

@app.route('/systems/download/csv')
@cortex.lib.user.login_required
def systems_download_csv():
	"""Downloads the list of allocated server names as a CSV file."""

	# Get the list of systems
	curd = cortex.lib.systems.get_systems(return_cursor=True, hide_inactive=False)

	# Return the response
	return Response(cortex.lib.systems.csv_stream(curd), mimetype="text/csv", headers={'Content-Disposition': 'attachment; filename="systems.csv"'})

################################################################################

@app.route('/systems/add', methods=['GET', 'POST'])
@cortex.lib.user.login_required
def systems_add_existing():
	"""Handles the Add Existing System page, which can be used to add missing
	systems to Cortex"""

	# Get the list of enabled classes
	classes = cortex.lib.classes.list(hide_disabled=True)

	# Get the list of Puppet environments
	puppet_envs = cortex.lib.core.get_puppet_environments()

	# On GET requests, just show the form
	if request.method == 'GET':
		return render_template('systems-add-existing.html', classes=classes, puppet_envs=puppet_envs, active='systems', title="Add existing system")

	# On POST requests...
	elif request.method == 'POST':
		if 'class' not in request.form or 'number' not in request.form or 'comment' not in request.form:
			abort(401)

		# Validate the class
		if request.form['class'].strip() not in [c['name'] for c in classes]:
			flash('You must choose a valid class', category='alert-danger')
			return render_template('systems-add-existing.html', classes=classes, puppet_envs=puppet_envs, active='systems', title="Add existing system")

		# Validate the number
		if len(request.form['number'].strip()) == 0:
			flash('You must enter a server number', category='alert-danger')
			return render_template('systems-add-existing.html', classes=classes, puppet_envs=puppet_envs, active='systems', title="Add existing system")

		# Validate the number
		if len(request.form['env'].strip()) != 0 and request.form['env'].strip() not in [e['puppet'] for e in puppet_envs]:
			flash('You must select either no Puppet environment or a valid Puppet environment (' + request.form['env'].strip() + ')', category='alert-danger')
			return render_template('systems-add-existing.html', classes=classes, puppet_envs=puppet_envs, active='systems', title="Add existing system")

		# Extract details
		class_name = request.form['class'].strip()
		number = int(request.form['number'].strip())
		comment = request.form['comment'].strip()
		puppet_env = request.form['env'].strip()

		# Get a cursor to the database
		curd = g.db.cursor(mysql.cursors.DictCursor)

		# Get the class
		curd.execute("SELECT * FROM `classes` WHERE `name` = %s", (class_name,))
		class_data = curd.fetchone()

		# Ensure we have a class
		if class_data is None:
			flash('Invalid class selected', category='alert-danger')
			return render_template('systems-add-existing.html', classes=classes, puppet_envs=puppet_envs, active='systems', title="Add existing system")

		# Generate the name, padded out correctly
		lib = NeoCortexLib.NeoCortexLib(g.db, app.config)
		generated_name = lib.pad_system_name(class_name, number, class_data['digits'])

		# Insert the system
		curd.execute("INSERT INTO `systems` (`type`, `class`, `number`, `name`, `allocation_date`, `allocation_who`, `allocation_comment`) VALUES (0, %s, %s, %s, NOW(), %s, %s)", (request.form['class'].strip(), request.form['number'].strip(), generated_name, session['username'], request.form['comment'].strip()))		

		# Insert the 
		if len(puppet_env) > 0:
			curd.execute("INSERT INTO `puppet_nodes` (`id`, `certname`, `env`, `include_default`, `classes`, `variables`) VALUES (%s, %s, %s, %s, %s, %s)", (curd.lastrowid, generated_name + ".soton.ac.uk", puppet_env, 1, "", ""))

		# Commit
		g.db.commit()
		
		# Redirect back to systems page
		flash("System added", "alert-success")
		return redirect(url_for('systems'))
			
################################################################################

@app.route('/systems/new', methods=['GET', 'POST'])
@cortex.lib.user.login_required
def systems_new():
	"""Handles the Allocate New System Name(s) page"""

	# On GET requests, just show big buttons for all the classes
	if request.method == 'GET':
		classes = cortex.lib.classes.list(hide_disabled=True)
		return render_template('systems-new.html', classes=classes, active='systems', title="Allocate new system names")

	# On POST requests...
	elif request.method == 'POST':
		# The user has asked for one or more new system names.

		# Grab the prefix chosen and validate it's a valid class name.
		# The regular expression matches on an entire string of 1 to 16 lowercase characters
		class_name = request.form['class_name']
		if not re.match(r'^[a-z]{1,16}$', class_name):
			flash("The class prefix you sent was invalid. It can only contain lowercase letters and be at least 1 character long and at most 16", "alert-danger")
			return redirect(url_for('systems_new'))

		# Grab how many names the user wants and validate it
		system_number = int(request.form['system_number'])

		# Validate the number of systems
		if system_number < 1 or system_number > 50:
			flash("You cannot allocate more than 50 names at once, sorry about that.", "alert-danger")
			return redirect(url_for('admin_classes'))

		# Grab the comment
		if 'system_comment' in request.form:
			system_comment = request.form['system_comment']
		else:
			system_comment = ""

		# Allocate the names asked for
		try:
			# To prevent code duplication, this is done remotely by Neocortex. So, connect:
			neocortex   = cortex.lib.core.neocortex_connect()

			# Allocate the name
			new_systems = neocortex.allocate_name(class_name, system_comment, username=session['username'], num=system_number)
		except Exception as ex:
			flash("A fatal error occured when trying to allocate names: " + str(ex), "alert-danger")
			return redirect(url_for('systems_new'))

		# If the user only wanted one system, redirect back to the systems
		# list page and flash up success. If they requested more than one
		# system, then redirect to a bulk-comments-edit page where they can
		# change the comments on all of the systems.
		if len(new_systems) == 1: 
			flash("System name allocated successfully", "alert-success")
			return redirect(url_for('systems_edit', id=new_systems[new_systems.keys()[0]]))
		else:
			return render_template('systems-new-bulk.html', systems=new_systems, comment=system_comment, title="Systems")

################################################################################

@app.route('/systems/bulk/save', methods=['POST'])
@cortex.lib.user.login_required
def systems_bulk_save():
	"""This is a POST handler used to set comments for a series of existing 
	systems which have been allocated already"""

	found_keys = []

	# Find a list of systems from the form. Each of the form input elements
	# containing a system comment has a name that starts "system_comment_"
	for key, value in request.form.iteritems():
		 if key.startswith("system_comment_"):
			# Yay we found one! blindly update it!
			updateid = key.replace("system_comment_", "")
			found_keys.append(updateid)
			cur = g.db.cursor()
			cur.execute("UPDATE `systems` SET `allocation_comment` = %s WHERE `id` = %s", (request.form[key], updateid))

	g.db.commit()

	flash("Comments successfully updated", "alert-success")
	return(redirect(url_for("systems_bulk_view", start=min(found_keys), finish=max(found_keys))))

################################################################################

@app.route('/systems/bulk/view/<int:start>/<int:finish>', methods=['GET'])
@cortex.lib.user.login_required
def systems_bulk_view(start, finish):
	"""This is a GET handler to view the list of assigned names"""

	start  = int(start)
	finish = int(finish)

	curd = g.db.cursor(mysql.cursors.DictCursor)
	curd.execute("SELECT `id`, `name`, `allocation_comment` AS `comment` FROM `systems` WHERE `id` >= %s AND `id` <= %s", (start, finish))
	systems = curd.fetchall()

	return render_template('systems-new-bulk-done.html', systems=systems, title="Systems")


################################################################################

@app.route('/systems/view/<int:id>', methods=['GET', 'POST'])
@cortex.lib.user.login_required
def systems_edit(id):
	# Get the system
	system = cortex.lib.systems.get_system_by_id(id)

	# Ensure that the system actually exists, and return a 404 if it doesn't
	if system is None:
		abort(404)

	if request.method == 'GET' or request.method == 'HEAD':
		# Get the system out of the database
		system_class = cortex.lib.classes.get(system['class'])
		system['review_status_text'] = cortex.lib.systems.REVIEW_STATUS_BY_ID[system['review_status']]

		if system['puppet_certname']:
			system['puppet_node_status'] = cortex.lib.puppet.puppetdb_get_node_status(system['puppet_certname'])
			
		return render_template('systems-edit.html', system=system, system_class=system_class, active='systems', title=system['name'])
	elif request.method == 'POST':
		try:
			# Get a cursor to the database
			curd = g.db.cursor(mysql.cursors.DictCursor)

			# Extract CMDB ID from form
			cmdb_id = request.form.get('cmdb_id',None)
			if cmdb_id is not None:			
				cmdb_id = cmdb_id.strip()
				if len(cmdb_id) == 0:
					cmdb_id = None

			# Extract VMware UUID from form
			vmware_uuid = request.form.get('vmware_uuid',None)
			if vmware_uuid is not None:			
				vmware_uuid = vmware_uuid.strip()
				if len(vmware_uuid) == 0:
					vmware_uuid = None

			# Extract Review Status from form
			review_status = request.form.get('review_status', 0)
			
			# Extract Review Ticket from form
			review_task = request.form.get('review_task', None)
			if review_task is not None:
				review_task = review_task.strip()
				if len(review_task) == 0:
					review_task = None

			# If the review status is "Under review" and a task hasn't been specified,
			# then we should create one.
			if int(review_status) == cortex.lib.systems.REVIEW_STATUS_BY_NAME['REVIEW'] and review_task is None:
				# Build some JSON
				task_data = {}
				task_data['time_constraint'] = 'asap'
				task_data['short_description'] = 'Review necessity of virtual machine ' + system['name']
				task_data['description'] = 'Please review the necessity of the virtual machine ' + system['name'] + ' to determine whether we need to keep it or whether it can be decommissioned. Information about the VM and links to ServiceNow can be found on Cortex at https://' + app.config['CORTEX_DOMAIN'] + url_for('systems_edit', id=id) + "\n\nOnce reviewed, please edit the system in Cortex using the link above and set it's 'Review Status' to either 'Required' or 'Not Required' and then close the associated project task."
				task_data['opened_by'] = app.config['REVIEW_TASK_OPENER_SYS_ID']
				task_data['assignment_group'] = app.config['REVIEW_TASK_TEAM']
				task_data['parent'] = app.config['REVIEW_TASK_PARENT_SYS_ID']

				# Make a post request to ServiceNow to create the task
				r = requests.post('https://' + app.config['SN_HOST'] + '/api/now/v1/table/pm_project_task', auth=(app.config['SN_USER'], app.config['SN_PASS']), headers={'Accept': 'application/json', 'Content-Type': 'application/json'}, json=task_data)

				# If we succeeded, get the task number
				if r is not None and r.status_code >= 200 and r.status_code <= 299:
					response_json = r.json()
					review_task = response_json['result']['number']
				else:
					error = "Failed to link ServiceNow task and CI."
					if r is not None:
						error = error + " HTTP Response code: " + str(r.status_code)
					raise Exception(error)

			# Update the system
			curd.execute('UPDATE `systems` SET `allocation_comment` = %s, `cmdb_id` = %s, `vmware_uuid` = %s, `review_status` = %s, `review_task` = %s WHERE `id` = %s', (request.form['allocation_comment'].strip(), cmdb_id, vmware_uuid, review_status, review_task, id))
			g.db.commit();

			flash('System updated', "alert-success") 
		except Exception as ex:
			# On error, notify the user
			flash('Failed to update system: ' + str(ex), 'alert-danger')

		# Regardless of success or error, redirect to the systems page
		return redirect(url_for('systems_edit', id=id))
	else:
		abort(400)

################################################################################

@app.route('/systems/vmware/json', methods=['POST'])
@cortex.lib.user.login_required
@app.disable_csrf_check
def systems_vmware_json():
	"""Used by DataTables to extract infromation from the VMware cache. The
	parameters and return format are dictated by DataTables"""

	# Extract information from DataTables
	(draw, start, length, order_column, order_asc, search) = _systems_extract_datatables()

	# Validate and extract ordering direction. 'asc' for ascending, 'desc' for
	# descending.
	if order_asc:
		order_dir = "ASC"
	else:
		order_dir = "DESC"

	# Validate and convert the ordering column number to the name of the
	# column as it is in the database
	if order_column == 0:
		order_column = 'name'
	elif order_column == 1:
		order_column = 'uuid'
	else:
		app.logger.warn('Invalid ordering column parameter in DataTables request')
		abort(400)

	# Query the database
	curd = g.db.cursor(mysql.cursors.DictCursor)

	# Get total number of VMs in cache
	curd.execute('SELECT COUNT(*) AS `count` FROM `vmware_cache_vm`;')
	total_count = curd.fetchone()['count']

	# Get total number of VMs that match query
	if search is not None:
		curd.execute('SELECT COUNT(*) AS `count` FROM `vmware_cache_vm` WHERE `name` LIKE %s', ("%" + search + "%"))
		filtered_count = curd.fetchone()['count']
	else:
		# If unfiltered, return the total count
		filtered_count = total_count

	# Build query	
	query = 'SELECT `name`, `uuid` FROM `vmware_cache_vm` '
	query_params = ()
	if search is not None:
		query = query + 'WHERE `name` LIKE %s '
		query_params = ("%" + search + "%")

	# Add on ordering
	query = query + "ORDER BY " + order_column + " " + order_dir + " "

	# Add on query limits
	query = query + "LIMIT " + str(start)
	if length is not None:
		query = query + "," + str(length)
	else:
		query = query + ",18446744073709551610"

	# Perform the query
	curd.execute(query, query_params)

	# Turn the results in to an appropriately shaped arrau
	row = curd.fetchone()
	system_data = []
	while row is not None:
		system_data.append([row['name'], row['uuid']])
		row = curd.fetchone()

	# Return JSON data in the format DataTables wants
	return jsonify(draw=draw, recordsTotal=total_count, recordsFiltered=filtered_count, data=system_data)


################################################################################

@app.route('/systems/cmdb/json', methods=['POST'])
@cortex.lib.user.login_required
@app.disable_csrf_check
def systems_cmdb_json():
	"""Used by DataTables to extract information from the ServiceNow CMDB CI
	cache. The parameters and return format are as dictated by DataTables"""

	# Extract information from DataTables
	(draw, start, length, order_column, order_asc, search) = _systems_extract_datatables()

	# Validate and convert the ordering column number to the name of the
	# column as it is in the database
	if order_column == 0:
		order_column = 'u_number'
	elif order_column == 1:
		order_column = 'short_description'
	else:
		app.logger.warn('Invalid ordering column parameter in DataTables request')
		abort(400)

	# Get results of query
	total_count    = cortex.lib.cmdb.get_ci_count()
	filtered_count = cortex.lib.cmdb.get_ci_count(search)
	results        = cortex.lib.cmdb.get_cis(start, length, search, order_column, order_asc)

	system_data = []
	for row in results:
		system_data.append([row['u_number'], row['name'], row['sys_id']])

	# Return JSON data in the format DataTables wants
	return jsonify(draw=draw, recordsTotal=total_count, recordsFiltered=filtered_count, data=system_data)

################################################################################

@app.route('/systems/json', methods=['POST'])
@cortex.lib.user.login_required
@app.disable_csrf_check
def systems_json():
	"""Used by DataTables to extract information from the systems table in
	the database. The parameters and return format are as dictated by 
	DataTables"""

	# Extract information from DataTables
	(draw, start, length, order_column, order_asc, search) = _systems_extract_datatables()

	# Validate and convert the ordering column number to the name of the
	# column as it is in the database
	if order_column == 0:
		order_column = 'name'
	elif order_column == 1:
		order_column = 'allocation_comment'
	elif order_column == 2:
		order_column = 'cmdb_environment'
	elif order_column == 3:
		order_column = 'allocation_who'
	elif order_column == 4:
		order_column = 'allocation_date'
	elif order_column == 5:
		order_column = 'cmdb_operational_status'
	else:
		app.logger.warn('Invalid ordering column parameter in DataTables request')
		abort(400)

	# Validate the system class filter group. This is the name of the
	# currently selected tab on the page that narrows down by system
	# class, e.g .srv, vhost, etc.
	filter_group = None
	if 'filter_group' in request.form:
		# The filtering on starting with * ignores some special filter groups
		if request.form['filter_group'] != '' and request.form['filter_group'][0] != '*':
			filter_group = str(request.form['filter_group'])

	# Filter group being *OTHER should hide our group names and filter on 
	only_other = False
	if request.form['filter_group'] == '*OTHER':
		only_other = True

	# Validate the flag for showing 'inactive' systems.
	hide_inactive = True
	if 'hide_inactive' in request.form:
		if str(request.form['hide_inactive']) == '0':
			hide_inactive = False

	# Get number of systems that match the query, and the number of systems
	# within the filter group
	system_count = cortex.lib.systems.get_system_count(filter_group, hide_inactive=hide_inactive, only_other=only_other)
	filtered_count = cortex.lib.systems.get_system_count(filter_group, search, hide_inactive, only_other=only_other)

	# Get results of query
	results = cortex.lib.systems.get_systems(filter_group, search, order_column, order_asc, start, length, hide_inactive, only_other)

	# DataTables wants an array in JSON, so we build this here, returning
	# only the columns we want. We format the date as a string as
	# datetime.datetime are not JSON-serialisable. We also add on columns for
	# CMDB ID and database ID, and operational status which are not displayed 
	# verbatim, but can be processed by a DataTables rowCallback
	system_data = []
	for row in results:
		if row['cmdb_id'] is not None and row['cmdb_id'] is not '':
			cmdb_id = app.config['CMDB_URL_FORMAT'] % row['cmdb_id']
		else:
			cmdb_id = ''

		if row['allocation_date'] is not None:
			row['allocation_date'] = row['allocation_date'].strftime('%Y-%m-%d %H:%M:%S')
		else:
			row['allocation_date'] = "Unknown"
		if row['allocation_who'] is not None:	
			row['allocation_who'] = cortex.lib.user.get_user_realname(row['allocation_who'])

		system_data.append([row['name'], row['allocation_comment'], row['cmdb_environment'], row['allocation_who'], row['allocation_date'], row['cmdb_operational_status'], cmdb_id, row['id'], row['vmware_guest_state'], row['puppet_certname']])

	# Return JSON data in the format DataTables wants
	return jsonify(draw=draw, recordsTotal=system_count, recordsFiltered=filtered_count, data=system_data)

################################################################################

def _systems_extract_datatables():
	# Validate and extract 'draw' parameter. This parameter is simply a counter
	# that DataTables uses internally.
	if 'draw' in request.form:
		draw = int(request.form['draw'])
	else:
		app.logger.warn('\'draw\' parameter missing from DataTables request')
		abort(400)

	# Validate and extract 'start' parameter. This parameter is the index of the
	# first row to return.
	if 'start' in request.form:
		start = int(request.form['start'])
	else:
		app.logger.warn('\'start\' parameter missing from DataTables request')
		abort(400)

	# Validate and extract 'length' parameter. This parameter is the number of
	# rows that we should return
	if 'length' in request.form:
		length = int(request.form['length'])
		if length < 0:
			length = None
	else:
		app.logger.warn('\'length\' parameter missing from DataTables request')
		abort(400)

	# Validate and extract ordering column. This parameter is the index of the
	# column on the HTML table to order by
	if 'order[0][column]' in request.form:
		order_column = int(request.form['order[0][column]'])
	else:
		order_column = 0

	# Validate and extract ordering direction. 'asc' for ascending, 'desc' for
	# descending.
	if 'order[0][dir]' in request.form:
		if request.form['order[0][dir]'] == 'asc':
			order_asc = True
		elif request.form['order[0][dir]'] == 'desc':
			order_asc = False
		else:
			app.logger.warn('Invalid \'order[0][dir]\' parameter in DataTables request')
			abort(400)
	else:
		order_asc = False

	# Handle the search parameter. This is the textbox on the DataTables
	# view that the user can search by typing in
	search = None
	if 'search[value]' in request.form:
		if request.form['search[value]'] != '':
			if type(request.form['search[value]']) is not str and type(request.form['search[value]']) is not unicode:
				search = str(request.form['search[value]'])
			else:
				search = request.form['search[value]']

	return (draw, start, length, order_column, order_asc, search)