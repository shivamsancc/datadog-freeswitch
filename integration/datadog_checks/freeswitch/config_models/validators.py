# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

# Here you can include additional config validators or transformers
#
# def initialize_instance(values, **kwargs):
#     if 'my_option' not in values and 'my_legacy_option' in values:
#         values['my_option'] = values['my_legacy_option']
#     if values.get('my_number') > 10:
#         raise ValueError('my_number max value is 10, got %s' % str(values.get('my_number')))
#
#     return values


def instance_host(value, **kwargs):
    # An empty host would be resolved by getaddrinfo() to the loopback address and produce an empty
    # freeswitch_host tag, silently monitoring the wrong endpoint.
    if isinstance(value, str) and not value.strip():
        raise ValueError('host must not be empty')

    return value
