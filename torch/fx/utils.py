from torch._logging import LazyString


def lazy_format_graph_code(name, gm, maybe_id=None):
    def format_name():
        if maybe_id is not None:
            return f"{name} {maybe_id}"
        else:
            return name

    return LazyString(
        lambda: _format_graph_code(
            f"===== {format_name()} =====\n",
            gm.forward.__code__.co_filename,
            gm.print_readable(print_output=False),
        )
    )


def _format_graph_code(name, filename, graph_str):
    return f"TRACED GRAPH\n {name} {filename} {graph_str}\n"
