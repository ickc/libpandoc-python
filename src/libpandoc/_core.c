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
    {"query", core_query, METH_VARARGS,
     "query(query: bytes) -> (status, output, error_kind, error_message, log)"},
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
    if (pandoc_abi_version() != LIBPANDOC_ABI_VERSION) {
        PyErr_Format(PyExc_ImportError,
                     "libpandoc ABI version %d, but this module was built for %d",
                     pandoc_abi_version(), LIBPANDOC_ABI_VERSION);
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
