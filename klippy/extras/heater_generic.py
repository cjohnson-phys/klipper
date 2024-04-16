# Support for a generic heater
#
# Copyright (C) 2019  John Jardine <john@gprime.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.


from klippy.configfile import ConfigWrapper


def load_config_prefix(config: ConfigWrapper):
	pheaters = config.get_printer().load_object(config, "heaters")
	return pheaters.setup_heater(config)
