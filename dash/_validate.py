import collections
import re

from .development.base_component import Component
from .dependencies import Input, Output, State, ANY, ALLSMALLER
from . import exceptions
from ._utils import patch_collections_abc, _strings, stringify_id


def validate_callback(app, layout, output, inputs, state):
    is_multi = isinstance(output, (list, tuple))
    validate_ids = not app.config.suppress_callback_exceptions

    if layout is None and validate_ids:
        # Without a layout, we can't do validation on the IDs and
        # properties of the elements in the callback.
        raise exceptions.LayoutIsNotDefined(
            """
            Attempting to assign a callback to the application but
            the `layout` property has not been assigned.
            Assign the `layout` property before assigning callbacks.
            Alternatively, suppress this warning by setting
            `suppress_callback_exceptions=True`
            """
        )

    if not inputs:
        raise exceptions.MissingInputsException(
            """
            This callback has no `Input` elements.
            Without `Input` elements, this callback will never get called.

            Subscribing to Input components will cause the
            callback to be called whenever their values change.
            """
        )

    outputs = output if is_multi else [output]

    for args, cls in [(outputs, Output), (inputs, Input), (state, State)]:
        validate_callback_args(args, cls, layout, validate_ids)

    prevent_duplicate_outputs(app, outputs)
    prevent_input_output_overlap(inputs, outputs)
    prevent_inconsistent_wildcards(outputs, inputs, state)


def validate_callback_args(args, cls, layout, validate_ids):
    name = cls.__name__
    if not isinstance(args, (list, tuple)):
        raise exceptions.IncorrectTypeException(
            """
            The {} argument `{}` must be a list or tuple of
            `dash.dependencies.{}`s.
            """.format(
                name.lower(), str(args), name
            )
        )

    for arg in args:
        if not isinstance(arg, cls):
            raise exceptions.IncorrectTypeException(
                """
                The {} argument `{}` must be of type `dash.dependencies.{}`.
                """.format(
                    name.lower(), str(arg), name
                )
            )

        if not isinstance(getattr(arg, "component_property", None), _strings):
            raise exceptions.IncorrectTypeException(
                """
                component_property must be a string, found {!r}
                """.format(
                    arg.component_property
                )
            )

        if hasattr(arg, "component_event"):
            raise exceptions.NonExistentEventException(
                """
                Events have been removed.
                Use the associated property instead.
                """
            )

        if isinstance(arg.component_id, dict):
            validate_id_dict(arg, layout, validate_ids, cls.allowed_wildcards)

        elif isinstance(arg.component_id, _strings):
            validate_id_string(arg, layout, validate_ids)

        else:
            raise exceptions.IncorrectTypeException(
                """
                component_id must be a string or dict, found {!r}
                """.format(
                    arg.component_id
                )
            )


def prevent_duplicate_outputs(app, outputs):
    for i, out in enumerate(outputs):
        for out2 in outputs[i + 1:]:
            if out == out2:
                # Note: different but overlapping wildcards compare as equal
                if str(out) == str(out2):
                    raise exceptions.DuplicateCallbackOutput(
                        """
                        Same output {} was used more than once in a callback!
                        """.format(
                            str(out)
                        )
                    )
                raise exceptions.DuplicateCallbackOutput(
                    """
                    Two outputs in a callback can match the same ID!
                    {} and {}
                    """.format(
                        str(out), str(out2)
                    )
                )

    dups = set()
    for out in outputs:
        for used_out in app.used_outputs:
            if out == used_out:
                dups.add(str(used_out))
    if dups:
        dups = list(dups)
        if len(outputs) > 1 or len(dups) > 1 or str(outputs[0]) != dups[0]:
            raise exceptions.DuplicateCallbackOutput(
                """
                One or more `Output` is already set by a callback.
                Note that two wildcard outputs can refer to the same component
                even if they don't match exactly.

                The new callback lists output(s):
                {}
                Already used:
                {}
                """.format(
                    ", ".join([str(out) for out in outputs]),
                    ", ".join(dups)
                )
            )
        raise exceptions.DuplicateCallbackOutput(
            """
            {} was already assigned to a callback.
            Any given output can only have one callback that sets it.
            Try combining your inputs and callback functions together
            into one function.
            """.format(
                repr(outputs[0])
            )
        )


def prevent_input_output_overlap(inputs, outputs):
    for in_ in inputs:
        for out in outputs:
            if out == in_:
                # Note: different but overlapping wildcards compare as equal
                if str(out) == str(in_):
                    raise exceptions.SameInputOutputException(
                        "Same `Output` and `Input`: {}".format(out)
                    )
                raise exceptions.SameInputOutputException(
                    """
                    An `Input` and an `Output` in one callback
                    can match the same ID!
                    {} and {}
                    """.format(
                        str(in_), str(out)
                    )
                )


def prevent_inconsistent_wildcards(outputs, inputs, state):
    any_keys = get_wildcard_keys(outputs[0], (ANY,))
    for out in outputs[1:]:
        if get_wildcard_keys(out, (ANY,)) != any_keys:
            raise exceptions.InconsistentCallbackWildcards(
                """
                All `Output` items must have matching wildcard `ANY` values.
                `ALL` wildcards need not match, only `ANY`.

                Output {} does not match the first output {}.
                """.format(
                    out, outputs[0]
                )
            )

    matched_wildcards = (ANY, ALLSMALLER)
    for dep in list(inputs) + list(state):
        wildcard_keys = get_wildcard_keys(dep, matched_wildcards)
        if wildcard_keys - any_keys:
            raise exceptions.InconsistentCallbackWildcards(
                """
                `Input` and `State` items can only have {}
                wildcards on keys where the `Output`(s) have `ANY` wildcards.
                `ALL` wildcards need not match, and you need not match every
                `ANY` in the `Output`(s).

                This callback has `ANY` on keys {}.
                {} has these wildcards on keys {}.
                """.format(
                    matched_wildcards, any_keys, dep, wildcard_keys
                )
            )


def validate_id_dict(arg, layout, validate_ids, wildcards):
    arg_id = arg.component_id

    def id_match(c):
        c_id = getattr(c, "id", None)
        return isinstance(c_id, dict) and all(
            k in c and v in wildcards or v == c_id.get(k)
            for k, v in arg_id.items()
        )

    if validate_ids:
        component = None
        if id_match(layout):
            component = layout
        else:
            for c in layout._traverse():  # pylint: disable=protected-access
                if id_match(c):
                    component = c
                    break
        if component:
            # for wildcards it's not unusual to have no matching components
            # initially; this isn't a problem and we shouldn't force users to
            # set suppress_callback_exceptions in this case; but if we DO have
            # a matching component, we can check that the prop is valid
            validate_prop_for_component(arg, component)

    for k, v in arg_id.items():
        if not (k and isinstance(k, _strings)):
            raise exceptions.IncorrectTypeException(
                """
                Wildcard ID keys must be non-empty strings,
                found {!r} in id {!r}
                """.format(
                    k, arg_id
                )
            )
        if not (v in wildcards or isinstance(v, _strings + (int, float, bool))):
            wildcard_msg = (
                ",\n                or wildcards: {}".format(wildcards)
                if wildcards else ""
            )
            raise exceptions.IncorrectTypeException(
                """
                Wildcard {} ID values must be strings, numbers, bools{}
                found {!r} in id {!r}
                """.format(
                    arg.__class__.__name__, wildcard_msg, k, arg_id
                )
            )


def validate_id_string(arg, layout, validate_ids):
    arg_id = arg.component_id

    invalid_chars = ".{"
    invalid_found = [x for x in invalid_chars if x in arg_id]
    if invalid_found:
        raise exceptions.InvalidComponentIdError(
            """
            The element `{}` contains `{}` in its ID.
            Characters `{}` are not allowed in IDs.
            """.format(
                arg_id, "`, `".join(invalid_found), "`, `".join(invalid_chars)
            )
        )

    if validate_ids:
        top_id = getattr(layout, "id", None)
        if arg_id not in layout and arg_id != top_id:
            raise exceptions.NonExistentIdException(
                """
                Attempting to assign a callback to the component with
                id "{}" but no components with that id exist in the layout.

                Here is a list of IDs in layout:
                {}

                If you are assigning callbacks to components that are
                generated by other callbacks (and therefore not in the
                initial layout), you can suppress this exception by setting
                `suppress_callback_exceptions=True`.
                """.format(
                    arg_id, [k for k in layout] + ([top_id] if top_id else [])
                )
            )

        component = layout if top_id == arg_id else layout[arg_id]
        validate_prop_for_component(arg, component)


def validate_prop_for_component(arg, component):
    arg_prop = arg.component_property
    if arg_prop not in component.available_properties and not any(
        arg_prop.startswith(w) for w in component.available_wildcard_properties
    ):
        raise exceptions.NonExistentPropException(
            """
            Attempting to assign a callback with the property "{0}"
            but component "{1}" doesn't have "{0}" as a property.

            Here are the available properties in "{1}":
            {2}
            """.format(
                arg_prop, arg.component_id, component.available_properties
            )
        )


def validate_multi_return(outputs_list, output_value, callback_id):
    if not isinstance(output_value, (list, tuple)):
        raise exceptions.InvalidCallbackReturnValue(
            """
            The callback {} is a multi-output.
            Expected the output type to be a list or tuple but got:
            {}.
            """.format(
                callback_id, repr(output_value)
            )
        )

    if len(output_value) != len(outputs_list):
        raise exceptions.InvalidCallbackReturnValue(
            """
            Invalid number of output values for {}.
            Expected {}, got {}
            """.format(
                callback_id, len(outputs_list), len(output_value)
            )
        )

    for i, outi in enumerate(outputs_list):
        if isinstance(outi, list):
            vi = output_value[i]
            if not isinstance(vi, (list, tuple)):
                raise exceptions.InvalidCallbackReturnValue(
                    """
                    The callback {} ouput {} is a wildcard multi-output.
                    Expected the output type to be a list or tuple but got:
                    {}.
                    """.format(
                        callback_id, i, repr(vi)
                    )
                )

            if len(vi) != len(outi):
                raise exceptions.InvalidCallbackReturnValue(
                    """
                    Invalid number of output values for {}.
                    Expected {}, got {}
                    """.format(
                        callback_id, len(vi), len(outi)
                    )
                )


def fail_callback_output(output_value, output):
    valid = _strings + (dict, int, float, type(None), Component)

    def _raise_invalid(bad_val, outer_val, path, index=None, toplevel=False):
        bad_type = type(bad_val).__name__
        outer_id = (
            "(id={:s})".format(outer_val.id) if getattr(outer_val, "id", False) else ""
        )
        outer_type = type(outer_val).__name__
        if toplevel:
            location = """
            The value in question is either the only value returned,
            or is in the top level of the returned list,
            """
        else:
            index_string = "[*]" if index is None else "[{:d}]".format(index)
            location = """
            The value in question is located at
            {} {} {}
            {},
            """.format(
                index_string, outer_type, outer_id, path
            )

        raise exceptions.InvalidCallbackReturnValue(
            """
            The callback for `{output}`
            returned a {object:s} having type `{type}`
            which is not JSON serializable.

            {location}
            and has string representation
            `{bad_val}`

            In general, Dash properties can only be
            dash components, strings, dictionaries, numbers, None,
            or lists of those.
            """.format(
                output=repr(output),
                object="tree with one value" if not toplevel else "value",
                type=bad_type,
                location=location,
                bad_val=bad_val,
            )
        )

    def _value_is_valid(val):
        return isinstance(val, valid)

    def _validate_value(val, index=None):
        # val is a Component
        if isinstance(val, Component):
            # pylint: disable=protected-access
            for p, j in val._traverse_with_paths():
                # check each component value in the tree
                if not _value_is_valid(j):
                    _raise_invalid(bad_val=j, outer_val=val, path=p, index=index)

                # Children that are not of type Component or
                # list/tuple not returned by traverse
                child = getattr(j, "children", None)
                if not isinstance(child, (tuple, collections.MutableSequence)):
                    if child and not _value_is_valid(child):
                        _raise_invalid(
                            bad_val=child,
                            outer_val=val,
                            path=p + "\n" + "[*] " + type(child).__name__,
                            index=index,
                        )

            # Also check the child of val, as it will not be returned
            child = getattr(val, "children", None)
            if not isinstance(child, (tuple, collections.MutableSequence)):
                if child and not _value_is_valid(child):
                    _raise_invalid(
                        bad_val=child,
                        outer_val=val,
                        path=type(child).__name__,
                        index=index,
                    )

        # val is not a Component, but is at the top level of tree
        elif not _value_is_valid(val):
            _raise_invalid(
                bad_val=val,
                outer_val=type(val).__name__,
                path="",
                index=index,
                toplevel=True,
            )

    if isinstance(output_value, list):
        for i, val in enumerate(output_value):
            _validate_value(val, index=i)
    else:
        _validate_value(output_value)

    # if we got this far, raise a generic JSON error
    raise exceptions.InvalidCallbackReturnValue(
        """
        The callback for property `{property:s}` of component `{id:s}`
        returned a value which is not JSON serializable.

        In general, Dash properties can only be dash components, strings,
        dictionaries, numbers, None, or lists of those.
        """.format(
            property=output.component_property, id=output.component_id
        )
    )


def get_wildcard_keys(dep, wildcards):
    _id = dep.component_id
    if not isinstance(_id, dict):
        return set()
    return {k for k, v in _id.items() if v in wildcards}


def check_obsolete(kwargs):
    for key in kwargs:
        if key in ["components_cache_max_age", "static_folder"]:
            raise exceptions.ObsoleteKwargException(
                """
                {} is no longer a valid keyword argument in Dash since v1.0.
                See https://dash.plot.ly for details.
                """.format(
                    key
                )
            )
        # any other kwarg mimic the built-in exception
        raise TypeError("Dash() got an unexpected keyword argument '" + key + "'")


def validate_js_path(registered_paths, package_name, path_in_package_dist):
    if package_name not in registered_paths:
        raise exceptions.DependencyException(
            """
            Error loading dependency. "{}" is not a registered library.
            Registered libraries are:
            {}
            """.format(
                package_name, list(registered_paths.keys())
            )
        )

    if path_in_package_dist not in registered_paths[package_name]:
        raise exceptions.DependencyException(
            """
            "{}" is registered but the path requested is not valid.
            The path requested: "{}"
            List of registered paths: {}
            """.format(
                package_name, path_in_package_dist, registered_paths
            )
        )


def validate_index(name, checks, index):
    missing = [i for check, i in checks if not re.compile(check).search(index)]
    if missing:
        plural = "s" if len(missing) > 1 else ""
        raise exceptions.InvalidIndexException(
            "Missing item{pl} {items} in {name}.".format(
                items=", ".join(missing), pl=plural, name=name
            )
        )


def validate_layout_type(value):
    if not isinstance(value, (Component, patch_collections_abc("Callable"))):
        raise exceptions.NoLayoutException(
            "Layout must be a dash component "
            "or a function that returns a dash component."
        )


def validate_layout(layout, layout_value):
    if layout is None:
        raise exceptions.NoLayoutException(
            """
            The layout was `None` at the time that `run_server` was called.
            Make sure to set the `layout` attribute of your application
            before running the server.
            """
        )

    layout_id = stringify_id(getattr(layout_value, "id", None))

    component_ids = {layout_id} if layout_id else set()
    for component in layout_value._traverse():  # pylint: disable=protected-access
        component_id = stringify_id(getattr(component, "id", None))
        if component_id and component_id in component_ids:
            raise exceptions.DuplicateIdError(
                """
                Duplicate component id found in the initial layout: `{}`
                """.format(
                    component_id
                )
            )
        component_ids.add(component_id)