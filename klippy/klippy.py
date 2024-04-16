#!/usr/bin/env python3
# Main code for host side printer firmware
#
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import sys, os, gc, optparse, logging, time, collections, importlib
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Type, Union

import util, reactor, queuelogger, msgproto
import gcode, configfile, pins, mcu, toolhead, webhooks

class MessageEnum(StrEnum):
	READY = "Printer is ready"
	STARTUP = """
Printer is not ready
The klippy host software is attempting to connect.  Please
retry in a few moments.
"""
	RESTART = """
Once the underlying issue is corrected, use the "RESTART"
command to reload the config and restart the host software.
Printer is halted
"""
	PROTOCOL_ERROR1 = """
This is frequently caused by running an older version of the
firmware on the MCU(s). Fix by recompiling and flashing the
firmware.
"""
	PROTOCOL_ERROR2 = """
Once the underlying issue is corrected, use the "RESTART"
command to reload the config and restart the host software.
"""
	MCU_CONNECT_ERROR = """
Once the underlying issue is corrected, use the
"FIRMWARE_RESTART" command to reset the firmware, reload the
config, and restart the host software.
Error configuring printer
"""
	SHUTDOWN = """
Once the underlying issue is corrected, use the
"FIRMWARE_RESTART" command to reset the firmware, reload the
config, and restart the host software.
Printer is shutdown
"""


class Printer:

	config_error = configfile.error
	command_error = gcode.CommandError

	def __init__(
			self,
			main_reactor: reactor.SelectReactor,
			bglogger: Optional[queuelogger.QueueListener],
			start_args: Dict[str, Union[str, int]]
		):
		self.bglogger = bglogger
		self.start_args = start_args
		self.reactor = main_reactor
		self.reactor.register_callback(self._connect)
		self.state_message: MessageEnum = MessageEnum.STARTUP
		self.in_shutdown_state: bool = False
		self.run_result: Optional[str] = None
		self.event_handlers = {}
		self.objects = collections.OrderedDict()
		# Init printer components that must be setup prior to config
		for m in [gcode, webhooks]:
			m.add_early_printer_objects(self)

	def get_start_args(self) -> Dict[str, Union[str, int]]:
		return self.start_args

	def get_reactor(self) -> reactor.SelectReactor:
		return self.reactor

	def get_state_message(
			self
		) -> Tuple[str, Literal['ready', 'startup', 'shutdown', 'error']]:
		if self.state_message == MessageEnum.READY:
			category = "ready"
		elif self.state_message == MessageEnum.STARTUP:
			category = "startup"
		elif self.in_shutdown_state:
			category = "shutdown"
		else:
			category = "error"
		return self.state_message, category

	def is_shutdown(self) -> bool:
		return self.in_shutdown_state

	def _set_state(self, msg: Union[str, MessageEnum]) -> None:
		if self.state_message in (MessageEnum.READY, MessageEnum.STARTUP):
			self.state_message = msg
		if msg != MessageEnum.READY and self.start_args.get("debuginput") is not None:
			self.request_exit("error_exit")

	def add_object(self, name: str, obj):
		if name in self.objects:
			raise self.config_error(f"Printer object '{name}' already created")
		self.objects[name] = obj

	def lookup_object(self, name: str, default=configfile.sentinel):
		if name in self.objects:
			return self.objects[name]
		if default is configfile.sentinel:
			raise self.config_error(f"Unknown config object '{name}'")
		return default

	def lookup_objects(self, module: Optional[str] = None) -> List[]:
		if module is None:
			return list(self.objects.items())
		prefix = module + " "
		objs = [(n, self.objects[n]) for n in self.objects if n.startswith(prefix)]
		if module in self.objects:
			return [(module, self.objects[module])] + objs
		return objs

	def load_object(
			self,
			config: configfile.ConfigWrapper,
			section: str,
			default: Optional[Type[configfile.sentinel]] = configfile.sentinel
		):
		if section in self.objects:
			return self.objects[section]
		module_parts = section.split()
		module_name = module_parts[0]
		py_name = os.path.join(os.path.dirname(__file__), "extras", module_name + ".py")
		py_dirname = os.path.join(
			os.path.dirname(__file__), "extras", module_name, "__init__.py"
		)
		if not os.path.exists(py_name) and not os.path.exists(py_dirname):
			if default is not configfile.sentinel:
				return default
			raise self.config_error(f"Unable to load module '{section}'")
		mod = importlib.import_module("extras." + module_name)
		init_func = "load_config"
		if len(module_parts) > 1:
			init_func = "load_config_prefix"
		init_func = getattr(mod, init_func, None)
		if init_func is None:
			if default is not configfile.sentinel:
				return default
			raise self.config_error(f"Unable to load module '{section}'")
		self.objects[section] = init_func(config.getsection(section))
		return self.objects[section]

	def _read_config(self):
		self.objects["configfile"] = pconfig = configfile.PrinterConfig(self)
		config = pconfig.read_main_config()
		if self.bglogger is not None:
			pconfig.log_config(config)
		# Create printer components
		for m in [pins, mcu]:
			m.add_printer_objects(config)
		for section_config in config.get_prefix_sections(""):
			self.load_object(config, section_config.get_name(), None)
		for m in [toolhead]:
			m.add_printer_objects(config)
		# Validate that there are no undefined parameters in the config file
		pconfig.check_unused_options(config)

	def _build_protocol_error_message(self, e) -> str:
		host_version = self.start_args["software_version"]
		msg_update = []
		msg_updated = []
		for mcu_name, mcu in self.lookup_objects("mcu"):
			try:
				mcu_version = mcu.get_status()["mcu_version"]
			except:
				logging.exception("Unable to retrieve mcu_version from mcu")
				continue
			if mcu_version != host_version:
				msg_update.append(
					f"{mcu_name.split()[-1]}: Current version {mcu_version}"
				)
			else:
				msg_updated.append(
					f"{mcu_name.split()[-1]}: Current version {mcu_version}"
				)
		if not msg_update:
			msg_update.append("<none>")
		if not msg_updated:
			msg_updated.append("<none>")
		msg = [
			"MCU Protocol error",
			MessageEnum.PROTOCOL_ERROR1.value,
			f"Your Klipper version is: {host_version}",
			"MCU(s) which should be updated:",
		]
		msg += msg_update + ["Up-to-date MCU(s):"] + msg_updated
		msg += [MessageEnum.PROTOCOL_ERROR2.value, str(e)]
		return "\n".join(msg)

	def _connect(self, eventtime) -> None:
		try:
			self._read_config()
			self.send_event("klippy:mcu_identify")
			for cb in self.event_handlers.get("klippy:connect", []):
				if self.state_message is not MessageEnum.STARTUP:
					return
				cb()
		except (self.config_error, pins.error) as e:
			logging.exception("Config error")
			self._set_state(f"{str(e)}\n{MessageEnum.RESTART.value}")
			return
		except msgproto.error as e:
			logging.exception("Protocol error")
			self._set_state(self._build_protocol_error_message(e))
			util.dump_mcu_build()
			return
		except mcu.error as e:
			logging.exception("MCU error during connect")
			self._set_state(f"{str(e)}{MessageEnum.MCU_CONNECT_ERROR.value}")
			util.dump_mcu_build()
			return
		except Exception as e:
			logging.exception("Unhandled exception during connect")
			self._set_state(
				f"Internal error during connect: "
				f"{str(e)}\n{MessageEnum.RESTART.value}"
			)
			return
		try:
			self._set_state(MessageEnum.READY)
			for cb in self.event_handlers.get("klippy:ready", []):
				if self.state_message is not MessageEnum.READY:
					return
				cb()
		except Exception as e:
			logging.exception("Unhandled exception during ready callback")
			self.invoke_shutdown(f"Internal error during ready callback: {str(e)}")

	def run(self) -> Optional[str]:
		systime = time.time()
		monotime = self.reactor.monotonic()
		logging.info(
			f"Start printer at {time.asctime(time.localtime(systime))} "
			f"({systime:.1f} {monotime:.1f})"
		)
		# Enter main reactor loop
		try:
			self.reactor.run()
		except:
			msg = "Unhandled exception during run"
			logging.exception(msg)
			# Exception from a reactor callback - try to shutdown
			try:
				self.reactor.register_callback((lambda e: self.invoke_shutdown(msg)))
				self.reactor.run()
			except:
				logging.exception("Repeat unhandled exception during run")
				# Another exception - try to exit
				self.run_result = "error_exit"
		# Check restart flags
		run_result = self.run_result
		try:
			if run_result == "firmware_restart":
				self.send_event("klippy:firmware_restart")
			self.send_event("klippy:disconnect")
		except:
			logging.exception("Unhandled exception during post run")
		return run_result

	def set_rollover_info(self, name: str, info: str, log=True) -> None:
		if log:
			logging.info(info)
		if self.bglogger is not None:
			self.bglogger.set_rollover_info(name, info)

	def invoke_shutdown(self, msg: str) -> None:
		if self.in_shutdown_state:
			return
		logging.error(f"Transition to shutdown state: {msg}")
		self.in_shutdown_state = True
		self._set_state(f"{msg}{MessageEnum.SHUTDOWN.value}")
		for cb in self.event_handlers.get("klippy:shutdown", []):
			try:
				cb()
			except:
				logging.exception("Exception during shutdown handler")
		logging.info(f"Reactor garbage collection: {self.reactor.get_gc_stats()}")

	def invoke_async_shutdown(self, msg: str) -> None:
		self.reactor.register_async_callback((lambda e: self.invoke_shutdown(msg)))

	def register_event_handler(self, event: str, callback: Callable):
		self.event_handlers.setdefault(event, []).append(callback)

	def send_event(self, event: str, *params):
		return [cb(*params) for cb in self.event_handlers.get(event, [])]

	def request_exit(self, result: str):
		if self.run_result is None:
			self.run_result = result
		self.reactor.end()


######################################################################
# Startup
######################################################################


def import_test():
	# Import all optional modules (used as a build test)
	dname = os.path.dirname(__file__)
	for mname in ["extras", "kinematics"]:
		for fname in os.listdir(os.path.join(dname, mname)):
			if fname.endswith(".py") and fname != "__init__.py":
				module_name = fname[:-3]
			else:
				iname = os.path.join(dname, mname, fname, "__init__.py")
				if not os.path.exists(iname):
					continue
				module_name = fname
			importlib.import_module(mname + "." + module_name)
	sys.exit(0)


def arg_dictionary(option, opt_str, value: str, parser: Dict[str, Any]):
	key, fname = "dictionary", value
	if "=" in value:
		mcu_name, fname = value.split("=", 1)
		key = "dictionary_" + mcu_name
	if parser.values.dictionary is None:
		parser.values.dictionary = {}
	parser.values.dictionary[key] = fname


def main():

	usage = "%prog [options] <config file>"
	opts = optparse.OptionParser(usage)
	opts.add_option(
		"-i",
		"--debuginput",
		dest="debuginput",
		type=Path,
		help="read commands from file instead of from tty port",
	)
	opts.add_option(
		"-I",
		"--input-tty",
		dest="inputtty",
		default=Path("/tmp/printer"),
		type=Path,
		help="input tty name (default is /tmp/printer)",
	)
	opts.add_option(
		"-a",
		"--api-server",
		dest="apiserver",
		type=Path,
		help="api server unix domain socket filename",
	)
	opts.add_option(
		"-l",
		"--logfile",
		dest="logfile",
		type=Path,
		help="write log to file instead of stderr"
	)
	opts.add_option(
		"-v",
		action="store_true",
		dest="verbose",
		help="enable debug messages"
	)
	opts.add_option(
		"-o",
		"--debugoutput",
		dest="debugoutput",
		type=Path,
		help="write output to file instead of to serial port",
	)
	opts.add_option(
		"-d",
		"--dictionary",
		dest="dictionary",
		type="string",
		action="callback",
		callback=arg_dictionary,
		help="file to read for mcu protocol dictionary",
	)
	opts.add_option(
		"--import-test",
		action="store_true",
		help="perform an import module test"
	)
	options, args = opts.parse_args()

	if options.import_test:
		import_test()
	if len(args) != 1:
		opts.error("Incorrect number of arguments")
	start_args: Dict[str, Union[str, int]] = {
		"config_file": args[0],
		"apiserver": str(options.apiserver.resolve()),
		"start_reason": "startup",
	}

	debuglevel = logging.INFO
	if options.verbose:
		debuglevel = logging.DEBUG

	if options.debuginput:
		start_args["debuginput"] = str(options.debuginput.resolve())
		debuginput = open(options.debuginput, "rb")
		start_args["gcode_fd"] = debuginput.fileno()
	else:
		start_args["gcode_fd"] = util.create_pty(options.inputtty)

	if options.debugoutput:
		start_args["debugoutput"] = str(options.debugoutput.resolve())
		start_args.update(options.dictionary)

	bglogger: Optional[queuelogger.QueueListener] = None
	if options.logfile:
		start_args["log_file"] = str(options.logfile.resolve())
		bglogger = queuelogger.setup_bg_logging(options.logfile, debuglevel)
	else:
		logging.getLogger().setLevel(debuglevel)

	logging.info("Starting Klippy...")
	git_info = util.get_git_version()
	git_vers = git_info["version"]
	extra_files = [
		fname
		for code, fname in git_info["file_status"]
		if (
			code in ("??", "!!")
			and fname.endswith(".py")
			and (
				fname.startswith("klippy/kinematics/")
				or fname.startswith("klippy/extras/")
			)
		)
	]
	modified_files = [
		fname for code, fname in git_info["file_status"] if code == "M"
	]
	extra_git_desc = ""
	if extra_files:
		if not git_vers.endswith("-dirty"):
			git_vers = git_vers + "-dirty"
		if len(extra_files) > 10:
			extra_files[10:] = [f"(+{len(extra_files) - 10} files)"]
		extra_git_desc += f'\nUntracked files: {", ".join(extra_files)}'
	if modified_files:
		if len(modified_files) > 10:
			modified_files[10:] = [f"(+{len(modified_files) - 10} files)"]
		extra_git_desc += f'\nModified files: {", ".join(modified_files)}'
	extra_git_desc += f'\nBranch: {git_info["branch"]}'
	extra_git_desc += f'\nRemote: {git_info["remote"]}'
	extra_git_desc += f'\nTracked URL: {git_info["url"]}'
	start_args["software_version"] = git_vers
	start_args["cpu_info"] = util.get_cpu_info()

	if bglogger is not None:
		versions = "\n".join(
			[
				f'Args: {sys.argv}',
				f'Git version: {repr(start_args["software_version"])}{extra_git_desc}',
				f'CPU: {start_args["cpu_info"]}',
				f'Python: {repr(sys.version)}',
			]
		)
		logging.info(versions)
	elif not options.debugoutput:
		logging.warning("No log file specified! Severe timing issues may result!")
	gc.disable()

	# Start Printer() class
	while True:
		if bglogger is not None:
			bglogger.clear_rollover_info()
			bglogger.set_rollover_info("versions", versions)
		gc.collect()
		main_reactor = reactor.Reactor(gc_checking=True)
		printer = Printer(main_reactor, bglogger, start_args)
		res = printer.run()
		if res in ["exit", "error_exit"]:
			break
		time.sleep(1.0)
		main_reactor.finalize()
		main_reactor = printer = None
		logging.info("Restarting printer")
		start_args["start_reason"] = res

	if bglogger is not None:
		bglogger.stop()

	if res == "error_exit":
		sys.exit(-1)


if __name__ == "__main__":
	main()
