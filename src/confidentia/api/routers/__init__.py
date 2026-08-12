"""Routers, one per family of platform tables.

The split follows the tables rather than a notion of "resources": every endpoint
is a query against something the pipeline already writes, so a router that did not
map onto a table would be a router computing a number of its own (ADR 0016).
"""
