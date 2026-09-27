/*
 * libpandoc._core: the CPython binding to libpandoc's C ABI.
 *
 * Built against the limited API (abi3), so one wheel per platform serves
 * every CPython >= 3.10. The GIL is released while pandoc runs, so
 * conversions in different Python threads run in parallel.
 *
 * Copyright (C) 2026 Kolen Cheung
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <stdlib.h>
#include <libpandoc.h>

/* Arguments are parsed as objects and read with PyBytes_AsStringAndSize,
 * not with "y#": under the limited API, which PyArg_ParseTuple symbol "#"
 * formats bind to depends on the Python whose headers built the module. */

/* A bytes object's buffer, or NULL (with *len 0) for None. */
static int
bytes_or_none(PyObject *o, const char **buf, Py_ssize_t *len)
{
    char *b;
    if (o == Py_None) {
        *buf = NULL;
        *len = 0;
        return 0;
    }
    if (PyBytes_AsStringAndSize(o, &b, len) < 0)
        return -1;
    *buf = b;
    return 0;
}

/* (status, output, error_kind, error_message, log) from a pandoc_result,
 * which is freed. */
static PyObject *
unpack(pandoc_result *r)
{
    PyObject *t;
    if (r == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "libpandoc: runtime failed to start");
        return NULL;
    }
    t = Py_BuildValue("(iNzzy)", r->status,
                      PyBytes_FromStringAndSize(r->output, (Py_ssize_t)r->output_len),
                      r->error_kind, r->error_message, r->log);
    Py_BEGIN_ALLOW_THREADS
    pandoc_result_free(r);
    Py_END_ALLOW_THREADS
    return t;
}

/* convert(options: bytes, input: bytes | None) */
static PyObject *
core_convert(PyObject *self, PyObject *args)
{
    PyObject *opts_obj, *input_obj;
    const char *opts, *input;
    Py_ssize_t opts_len, input_len;
    pandoc_result *r;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!O", &PyBytes_Type, &opts_obj, &input_obj)
        || bytes_or_none(opts_obj, &opts, &opts_len) < 0
        || bytes_or_none(input_obj, &input, &input_len) < 0)
        return NULL;
    Py_BEGIN_ALLOW_THREADS
    r = pandoc_convert(opts, (size_t)opts_len, input, (size_t)input_len);
    Py_END_ALLOW_THREADS
    return unpack(r);
}

/* convert_args(args: tuple[bytes, ...], input: bytes | None) */
static PyObject *
core_convert_args(PyObject *self, PyObject *args)
{
    PyObject *argv_tuple, *input_obj, *result = NULL;
    const char *input;
    Py_ssize_t input_len, n, i;
    const char **argv;
    pandoc_result *r;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!O", &PyTuple_Type, &argv_tuple, &input_obj)
        || bytes_or_none(input_obj, &input, &input_len) < 0)
        return NULL;
    n = PyTuple_Size(argv_tuple);
    argv = (const char **)calloc((size_t)n + 1, sizeof(char *));
    if (argv == NULL)
        return PyErr_NoMemory();
    for (i = 0; i < n; i++) {
        PyObject *item = PyTuple_GetItem(argv_tuple, i);
        char *s;
        Py_ssize_t len;
        if (item == NULL || PyBytes_AsStringAndSize(item, &s, &len) < 0)
            goto done;
        argv[i] = s;
    }
    /* argv borrows from the tuple, which the caller keeps alive */
    Py_BEGIN_ALLOW_THREADS
    r = pandoc_convert_args((int)n, argv, input, (size_t)input_len);
    Py_END_ALLOW_THREADS
    result = unpack(r);
done:
    free(argv);
    return result;
}

/* Filters in Python, called back by pandoc inside a conversion (on this
 * thread, with the GIL released by convert_filters, so taken again here).
 * The first exception a filter raises is kept, to be raised by
 * convert_filters instead of pandoc's error. */
typedef struct {
    PyObject *type, *value, *traceback;
} saved_error;

typedef struct {
    PyObject *fn;
    saved_error *error;
} py_filter;

static int
call_py_filter(void *userdata, const char *doc, size_t doc_len,
               const char *context, size_t context_len, pandoc_buffer *out)
{
    py_filter *f = (py_filter *)userdata;
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject *d = NULL, *c = NULL, *r = NULL;
    char *buf;
    Py_ssize_t len;
    int status = 1;

    d = PyBytes_FromStringAndSize(doc, (Py_ssize_t)doc_len);
    c = PyBytes_FromStringAndSize(context, (Py_ssize_t)context_len);
    if (d != NULL && c != NULL)
        r = PyObject_CallFunctionObjArgs(f->fn, d, c, NULL);
    if (r != NULL && PyBytes_AsStringAndSize(r, &buf, &len) == 0) {
        pandoc_buffer_set(out, buf, (size_t)len);
        status = 0;
    } else {
        /* pandoc's message for the error; the exception itself is raised
         * by convert_filters */
        PyObject *type, *value, *tb, *msg = NULL;
        PyErr_Fetch(&type, &value, &tb);
        PyErr_NormalizeException(&type, &value, &tb);
        if (value != NULL)
            msg = PyObject_Str(value);
        if (msg != NULL) {
            Py_ssize_t n;
            const char *m = PyUnicode_AsUTF8AndSize(msg, &n);
            if (m != NULL)
                pandoc_buffer_set(out, m, (size_t)n);
            Py_DECREF(msg);
        }
        PyErr_Clear();
        if (f->error->type == NULL) {
            f->error->type = type;
            f->error->value = value;
            f->error->traceback = tb;
        } else {
            Py_XDECREF(type);
            Py_XDECREF(value);
            Py_XDECREF(tb);
        }
    }
    Py_XDECREF(d);
    Py_XDECREF(c);
    Py_XDECREF(r);
    PyGILState_Release(gil);
    return status;
}

/* The pandoc_filter array for a tuple of Python callables; the callables
 * are borrowed from the tuple, which the caller keeps alive. */
static int
make_filters(PyObject *fns, saved_error *error, pandoc_filter **filters,
             py_filter **data, Py_ssize_t *n)
{
    Py_ssize_t i;
    *n = PyTuple_Size(fns);
    *filters = (pandoc_filter *)calloc((size_t)*n + 1, sizeof(pandoc_filter));
    *data = (py_filter *)calloc((size_t)*n + 1, sizeof(py_filter));
    if (*filters == NULL || *data == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (i = 0; i < *n; i++) {
        (*data)[i].fn = PyTuple_GetItem(fns, i);
        (*data)[i].error = error;
        (*filters)[i].fn = call_py_filter;
        (*filters)[i].userdata = &(*data)[i];
    }
    return 0;
}

/* The result of a conversion with Python filters: a filter's exception if
 * one raised, else as unpack. */
static PyObject *
unpack_filtered(pandoc_result *r, saved_error *error)
{
    if (error->type != NULL) {
        Py_BEGIN_ALLOW_THREADS
        pandoc_result_free(r);
        Py_END_ALLOW_THREADS
        PyErr_Restore(error->type, error->value, error->traceback);
        return NULL;
    }
    return unpack(r);
}

/* convert_filters(options: bytes, input: bytes | None,
 *                 filters: tuple[Callable[[bytes, bytes], bytes], ...])
 * The options refer to filters[i] as {"type": "callback", "index": i}. */
static PyObject *
core_convert_filters(PyObject *self, PyObject *args)
{
    PyObject *opts_obj, *input_obj, *fns, *result = NULL;
    const char *opts, *input;
    Py_ssize_t opts_len, input_len, n;
    pandoc_filter *filters = NULL;
    py_filter *data = NULL;
    saved_error error = {NULL, NULL, NULL};
    pandoc_result *r;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!OO!", &PyBytes_Type, &opts_obj, &input_obj,
                          &PyTuple_Type, &fns)
        || bytes_or_none(opts_obj, &opts, &opts_len) < 0
        || bytes_or_none(input_obj, &input, &input_len) < 0)
        return NULL;
    if (make_filters(fns, &error, &filters, &data, &n) == 0) {
        Py_BEGIN_ALLOW_THREADS
        r = pandoc_convert_filters(opts, (size_t)opts_len, input, (size_t)input_len,
                                   filters, (size_t)n);
        Py_END_ALLOW_THREADS
        result = unpack_filtered(r, &error);
    }
    free(filters);
    free(data);
    return result;
}

/* convert_args_filters(args: tuple[bytes, ...], input: bytes | None,
 *                      filters: tuple[Callable[[bytes, bytes], bytes], ...])
 * The arguments refer to filters[i] as --lua-filter=libpandoc:callback/i. */
static PyObject *
core_convert_args_filters(PyObject *self, PyObject *args)
{
    PyObject *argv_tuple, *input_obj, *fns, *result = NULL;
    const char *input;
    Py_ssize_t input_len, argc, i, n;
    const char **argv = NULL;
    pandoc_filter *filters = NULL;
    py_filter *data = NULL;
    saved_error error = {NULL, NULL, NULL};
    pandoc_result *r;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!OO!", &PyTuple_Type, &argv_tuple, &input_obj,
                          &PyTuple_Type, &fns)
        || bytes_or_none(input_obj, &input, &input_len) < 0)
        return NULL;
    argc = PyTuple_Size(argv_tuple);
    argv = (const char **)calloc((size_t)argc + 1, sizeof(char *));
    if (argv == NULL)
        return PyErr_NoMemory();
    for (i = 0; i < argc; i++) {
        PyObject *item = PyTuple_GetItem(argv_tuple, i);
        char *s;
        Py_ssize_t len;
        if (item == NULL || PyBytes_AsStringAndSize(item, &s, &len) < 0)
            goto done;
        argv[i] = s;
    }
    if (make_filters(fns, &error, &filters, &data, &n) == 0) {
        Py_BEGIN_ALLOW_THREADS
        r = pandoc_convert_args_filters((int)argc, argv, input, (size_t)input_len,
                                        filters, (size_t)n);
        Py_END_ALLOW_THREADS
        result = unpack_filtered(r, &error);
    }
done:
    free(argv);
    free(filters);
    free(data);
    return result;
}

/* query(query: bytes) */
static PyObject *
core_query(PyObject *self, PyObject *args)
{
    PyObject *q_obj;
    const char *q;
    Py_ssize_t q_len;
    pandoc_result *r;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!", &PyBytes_Type, &q_obj)
        || bytes_or_none(q_obj, &q, &q_len) < 0)
        return NULL;
    Py_BEGIN_ALLOW_THREADS
    r = pandoc_query(q, (size_t)q_len);
    Py_END_ALLOW_THREADS
    return unpack(r);
}

/* main(argv: tuple[bytes, ...], filters: bytes | None,
 *      callbacks: tuple[Callable[[bytes, bytes], bytes], ...]) -> int
 * The pandoc command in this process (argv[0] is the program's name); a
 * callback's exception is raised after pandoc has reported the failure. */
static PyObject *
core_main(PyObject *self, PyObject *args)
{
    PyObject *argv_tuple, *filters_obj, *fns, *result = NULL;
    const char *fjson;
    Py_ssize_t fjson_len, argc, i, n;
    const char **argv = NULL;
    pandoc_filter *filters = NULL;
    py_filter *data = NULL;
    saved_error error = {NULL, NULL, NULL};
    int status;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!OO!", &PyTuple_Type, &argv_tuple, &filters_obj,
                          &PyTuple_Type, &fns)
        || bytes_or_none(filters_obj, &fjson, &fjson_len) < 0)
        return NULL;
    argc = PyTuple_Size(argv_tuple);
    argv = (const char **)calloc((size_t)argc + 1, sizeof(char *));
    if (argv == NULL)
        return PyErr_NoMemory();
    for (i = 0; i < argc; i++) {
        PyObject *item = PyTuple_GetItem(argv_tuple, i);
        char *s;
        Py_ssize_t len;
        if (item == NULL || PyBytes_AsStringAndSize(item, &s, &len) < 0)
            goto done;
        argv[i] = s;
    }
    if (make_filters(fns, &error, &filters, &data, &n) == 0) {
        Py_BEGIN_ALLOW_THREADS
        status = pandoc_main((int)argc, argv, fjson, (size_t)fjson_len, filters, (size_t)n);
        Py_END_ALLOW_THREADS
        if (error.type != NULL)
            PyErr_Restore(error.type, error.value, error.traceback);
        else
            result = PyLong_FromLong(status);
    }
done:
    free(argv);
    free(filters);
    free(data);
    return result;
}

/* read_many(request: bytes) */
static PyObject *
core_read_many(PyObject *self, PyObject *args)
{
    PyObject *q_obj;
    const char *q;
    Py_ssize_t q_len;
    pandoc_result *r;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!", &PyBytes_Type, &q_obj)
        || bytes_or_none(q_obj, &q, &q_len) < 0)
        return NULL;
    Py_BEGIN_ALLOW_THREADS
    r = pandoc_read_many(q, (size_t)q_len);
    Py_END_ALLOW_THREADS
    return unpack(r);
}

/* set_num_threads(n: int) -> int */
static PyObject *
core_set_num_threads(PyObject *self, PyObject *args)
{
    int n, r;
    (void)self;
    if (!PyArg_ParseTuple(args, "i", &n))
        return NULL;
    Py_BEGIN_ALLOW_THREADS
    r = pandoc_set_num_threads(n);
    Py_END_ALLOW_THREADS
    return PyLong_FromLong(r);
}

static PyObject *
core_abi_version(PyObject *self, PyObject *noargs)
{
    (void)self; (void)noargs;
    return PyLong_FromLong(pandoc_abi_version());
}

static PyMethodDef core_methods[] = {
    {"convert", core_convert, METH_VARARGS,
     "convert(options: bytes, input: bytes | None) -> (status, output, error_kind, error_message, log)"},
    {"convert_args", core_convert_args, METH_VARARGS,
     "convert_args(args: tuple[bytes, ...], input: bytes | None) -> (status, output, error_kind, error_message, log)"},
    {"convert_filters", core_convert_filters, METH_VARARGS,
     "convert_filters(options: bytes, input: bytes | None, filters: tuple[Callable[[bytes, bytes], bytes], ...]) -> (status, output, error_kind, error_message, log)"},
    {"convert_args_filters", core_convert_args_filters, METH_VARARGS,
     "convert_args_filters(args: tuple[bytes, ...], input: bytes | None, filters: tuple[Callable[[bytes, bytes], bytes], ...]) -> (status, output, error_kind, error_message, log)"},
    {"main", core_main, METH_VARARGS,
     "main(argv: tuple[bytes, ...], filters: bytes | None, callbacks: tuple[...]) -> int: the pandoc command"},
    {"read_many", core_read_many, METH_VARARGS,
     "read_many(request: bytes) -> (status, output, error_kind, error_message, log)"},
    {"query", core_query, METH_VARARGS,
     "query(query: bytes) -> (status, output, error_kind, error_message, log)"},
    {"set_num_threads", core_set_num_threads, METH_VARARGS,
     "set_num_threads(n: int) -> int: pandoc's threads from now on; the new number"},
    {"abi_version", core_abi_version, METH_NOARGS,
     "abi_version() -> int: LIBPANDOC_ABI_VERSION of the loaded library"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef core_module = {
    PyModuleDef_HEAD_INIT, "libpandoc._core",
    "Low-level binding to libpandoc. Use the libpandoc package instead.",
    -1, core_methods, NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC
PyInit__core(void)
{
    PyObject *m;
    /* the same major version, and at least the minor one built against */
    int abi = pandoc_abi_version();
    if (abi / 1000 != LIBPANDOC_ABI_VERSION_MAJOR || abi % 1000 < LIBPANDOC_ABI_VERSION_MINOR) {
        PyErr_Format(PyExc_ImportError,
                     "libpandoc's C interface is version %d.%d, but this module needs "
                     "%d.%d or a later %d.x",
                     abi / 1000, abi % 1000, LIBPANDOC_ABI_VERSION_MAJOR,
                     LIBPANDOC_ABI_VERSION_MINOR, LIBPANDOC_ABI_VERSION_MAJOR);
        return NULL;
    }
    m = PyModule_Create(&core_module);
    if (m == NULL)
        return NULL;
    if (PyModule_AddIntConstant(m, "ABI_VERSION", LIBPANDOC_ABI_VERSION) < 0) {
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
