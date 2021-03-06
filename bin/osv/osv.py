# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2004-2009 Tiny SPRL (<http://tiny.be>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

#.apidoc title: Objects Services (OSV)

import orm
import logging
import netsvc
import pooler
import copy
from psycopg2 import IntegrityError, errorcodes
from tools.func import wraps
from tools.translate import translate

module_list = []
module_class_list = {}
class_pool = {}

class except_osv(Exception):
    def __init__(self, name, value, exc_type='warning'):
        self.name = name
        self.exc_type = exc_type
        self.value = value
        self.args = (exc_type, name)


class object_proxy(netsvc.Service):
    def __init__(self):
        self.logger = logging.getLogger('web-services')
        netsvc.Service.__init__(self, 'object_proxy', audience='')
        self.exportMethod(self.exec_workflow)
        self.exportMethod(self.execute)
        self.exportMethod(self.exec_dict)
        self.exportMethod(self.set_debug)
        self.exportMethod(self.obj_list)

    def check(f):
        @wraps(f)
        def wrapper(self, dbname, *args, **kwargs):
            """ Wraps around OSV functions and normalises a few exceptions
            """

            def tr(src, ttype):
                # We try to do the same as the _(), but without the frame
                # inspection, since we aready are wrapping an osv function
                # trans_obj = self.get('ir.translation') cannot work yet :(
                ctx = {}
                if not kwargs:
                    if args and isinstance(args[-1], dict):
                        ctx = args[-1]
                elif isinstance(kwargs, dict):
                    ctx = kwargs.get('context', {})

                uid = 1
                if args and isinstance(args[0], (long, int)):
                    uid = args[0]

                lang = ctx and ctx.get('lang')
                if not (lang or hasattr(src, '__call__')):
                    return src

                # We open a *new* cursor here, one reason is that failed SQL
                # queries (as in IntegrityError) will invalidate the current one.
                cr = False
                
                if hasattr(src, '__call__'):
                    # callable. We need to find the right parameters to call
                    # the  orm._sql_message(self, cr, uid, ids, context) function,
                    # or we skip..
                    # our signature is f(osv_pool, dbname [,uid, obj, method, args])
                    try:
                        if args and len(args) > 1:
                            obj = self.get(args[1])
                            if len(args) > 3 and isinstance(args[3], (long, int, list)):
                                ids = args[3]
                            else:
                                ids = []
                        cr = pooler.get_db_only(dbname).cursor()
                        return src(obj, cr, uid, ids, context=(ctx or {}))
                    except Exception:
                        pass
                    finally:
                        if cr: cr.close()
                   
                    return False # so that the original SQL error will
                                 # be returned, it is the best we have.

                try:
                    cr = pooler.get_db_only(dbname).cursor()
                    res = translate(cr, name=False, source_type=ttype,
                                    lang=lang, source=src)
                    if res:
                        return res
                    else:
                        return src
                finally:
                    if cr: cr.close()

            def _(src):
                return tr(src, 'code')

            try:
                if not pooler.get_pool(dbname)._ready:
                    raise except_osv('Database not ready', 'Currently, this database is not fully loaded and can not be used.')
                return f(self, dbname, *args, **kwargs)
            except orm.except_orm, inst:
                if inst.name == 'AccessError':
                    self.logger.debug("AccessError", exc_info=True)
                self.abortResponse(1, inst.name, 'warning', inst.value)
            except except_osv, inst:
                self.abortResponse(1, inst.name, inst.exc_type, inst.value)
            except IntegrityError, inst:
                osv_pool = pooler.get_pool(dbname)
                for key in osv_pool._sql_error.keys():
                    if key in inst[0]:
                        self.abortResponse(1, _('Constraint Error'), 'warning',
                                        tr(osv_pool._sql_error[key], 'sql_constraint') or inst[0])
                if inst.pgcode in (errorcodes.NOT_NULL_VIOLATION, errorcodes.FOREIGN_KEY_VIOLATION, errorcodes.RESTRICT_VIOLATION):
                    msg = inst.pgerror + '\n'
                    msg += _('The operation cannot be completed, probably due to the following:\n' \
                          '- deletion: you may be trying to delete a record while other records still reference it\n' \
                          '- creation/update: a mandatory field is not correctly set' )
                    self.logger.debug("IntegrityError", exc_info=False)
                    try:
                        if '"public".' in inst.pgerror:
                            context = inst.pgerror.split('"public".')[1]
                            model_name = table = context.split('"')[1]
                            model = table.replace("_",".")
                            model_obj = osv_pool.get(model)
                            if model_obj:
                                model_name = model_obj._description or model_obj._name
                            msg += _('\n\n[object with reference: %s - %s]') % (model_name, model)
                    except Exception:
                        pass
                    self.abortResponse(1, _('Integrity Error'), 'warning', msg)
                else:
                    self.abortResponse(1, _('Integrity Error'), 'warning', inst[0])
            except Exception:
                self.logger.exception("Uncaught exception")
                raise

        return wrapper

    def _get_cr_auth(self, dbname, kwargs=None):
        """ retrieve the cursor, but also decorate it with caller info from kwargs['auth_proxy']

            The overlying `objects_proxy` of `web_services` will call these methods
            with 'auth_proxy' mirrored in kwargs. If so, we keep a weak reference
            to that in cursor (that's `cr` all over the ORM code).
        """
        cr = pooler.get_db(dbname).cursor()

        if kwargs and 'auth_proxy' in kwargs:
            cr.auth_proxy = kwargs.pop('auth_proxy')

        return cr

    def execute_cr(self, cr, uid, obj, method, *args, **kw):
        object = pooler.get_pool(cr.dbname).get(obj)
        if not object:
            raise except_osv('Object Error', 'Object %s doesn\'t exist' % str(obj))
        try:
            return getattr(object, method)(cr, uid, *args, **kw)
        except TypeError:
            try:
                import inspect
                fn = getattr(object, method)
                __fn_name = method
                __fn_file = inspect.getfile(fn)
                tb_s = "Function %s from file %s" %( __fn_name, __fn_file)
            except Exception:
                tb_s = "Object: %s Function: %s\n" % (object, getattr(object, method,'<%s ?>' % method))
            self.logger.exception(tb_s)
            raise

    @check
    def execute(self, db, uid, obj, method, *args, **kw):
        cr = self._get_cr_auth(db, kw)
        try:
            try:
                if method.startswith('_'):
                    raise except_osv('Access Denied', 'Private methods (such as %s) cannot be called remotely.' % (method,))
                res = self.execute_cr(cr, uid, obj, method, *args, **kw)
                if res is None:
                    self.logger.warning('Method %s.%s can not return a None value (crash in XML-RPC)', obj, method)
                cr.commit()
            except Exception:
                cr.rollback()
                raise
        finally:
            cr.close()
        return res

    @check
    def exec_dict(self, db, uid, obj, method, args, kwargs=None, **kw):
        """ Execute some ORM call, with support of both positional and keyword arguments
            Note that we strictly expect *2* positional arguments here, one list for
            args and one dict for kwargs
        """
        if not isinstance(args, list):
            raise ValueError("exec_dict() must be called with (args:list, kwargs: dict)")
        if kwargs is None:
            kwargs = {}
        elif not isinstance(kwargs, dict):
            raise ValueError("exec_dict() must be called with (args:list, kwargs: dict)")
        
        cr = self._get_cr_auth(db, kw)
        try:
            try:
                if method.startswith('_'):
                    raise except_osv('Access Denied', 'Private methods (such as %s) cannot be called remotely.', method)
                res = self.execute_cr(cr, uid, obj, method, *args, **kwargs)
                if res is None:
                    self.logger.warning('Method %s.%s can not return a None value (crash in XML-RPC)', obj, method)
                cr.commit()
            except Exception:
                cr.rollback()
                raise
        finally:
            cr.close()
        return res

    def exec_workflow_cr(self, cr, uid, obj, method, *args):
        wf_service = netsvc.LocalService("workflow")
        return wf_service.trg_validate(uid, obj, args[0], method, cr)

    @check
    def exec_workflow(self, db, uid, obj, method, *args, **kw):
        cr = self._get_cr_auth(db, kw)
        try:
            try:
                res = self.exec_workflow_cr(cr, uid, obj, method, *args)
                cr.commit()
            except Exception:
                cr.rollback()
                raise
        finally:
            cr.close()
        return res

    def set_debug(self, db, obj, do_debug=True, **kw):
        object = pooler.get_pool(db).get(obj)
        if not object:
            raise except_osv('Object Error', 'Object %s doesn\'t exist' % str(obj))
        try:
            object._debug = do_debug
            return True
        except Exception:
            raise

    def obj_list(self, **kw):
        return []

object_proxy()

class osv_pool(object):
    def __init__(self):
        self._ready = False
        self.obj_pool = {}
        self.module_object_list = {}
        self.created = []
        self._sql_error = {}
        self._store_function = {}
        self._init = True
        self._init_parent = {}
        self.logger = logging.getLogger("pool")
        #: Store some values, temprarily, for the init phase
        self._init_values = {}

    def init_set(self, cr, mode):
        different = mode != self._init
        if different:
            if mode:
                self._init_parent = {}
            if not mode:
                for o in self._init_parent:
                    self.get(o)._parent_store_compute(cr)
            self._init = mode

        self._ready = True
        return different


    def obj_list(self):
        return self.obj_pool.keys()

    # adds a new object instance to the object pool.
    # if it already existed, the instance is replaced
    def add(self, name, obj_inst):
        assert isinstance(name, basestring)
        if name in self.obj_pool:
            del self.obj_pool[name]
        self.obj_pool[name] = obj_inst

        module = str(obj_inst.__class__)[6:]
        module = module[:len(module)-1]
        module = module.split('.')[0][2:]
        self.module_object_list.setdefault(module, []).append(obj_inst)

    # Return None if object does not exist
    def get(self, name):
        if not name:
            return None
        assert isinstance(name, basestring), repr(name)
        obj = self.obj_pool.get(name, None)
        if not obj:
            self.logger.warning("Object %s not found by pooler!", name)
        return obj

    #TODO: pass a list of modules to load
    def instanciate(self, module, cr):
        res = []
        class_list = module_class_list.get(module, [])
        for klass in class_list:
            res.append(klass.createInstance(self, module, cr))
        return res

class osv_base(object):
    def __init__(self, pool, cr):
        pool.add(self._name, self)
        self.pool = pool
        super(osv_base, self).__init__(cr)

    def __new__(cls):
        module = str(cls)[6:]
        module = module[:len(module)-1]
        module = module.split('.')[0][2:]
        if not hasattr(cls, '_module'):
            cls._module = module
        module_class_list.setdefault(cls._module, []).append(cls)
        class_pool[cls._name] = cls
        if module not in module_list:
            module_list.append(cls._module)
        return None

class osv_memory(osv_base, orm.orm_memory):
    #
    # Goal: try to apply inheritancy at the instanciation level and
    #       put objects in the pool var
    #
    def createInstance(cls, pool, module, cr):
        parent_names = getattr(cls, '_inherit', None)
        if parent_names:
            if isinstance(parent_names, (str, unicode)):
                name = cls._name or parent_names
                parent_names = [parent_names]
            else:
                name = cls._name
            if not name:
                raise TypeError('_name is mandatory in case of multiple inheritance')

            for parent_name in ((type(parent_names)==list) and parent_names or [parent_names]):
                parent_class = pool.get(parent_name).__class__
                assert pool.get(parent_name), "parent class %s does not exist in module %s !" % (parent_name, module)
                nattr = {}
                for s in ('_columns', '_defaults', '_virtuals', '_vtable'):
                    new = copy.copy(getattr(pool.get(parent_name), s))
                    if s == '_columns':
                        new_cols = cls.__dict__.get(s, {})
                        for nn, nc in new_cols.items():
                            if isinstance(nc, orm.fields.inherit):
                                if parent_name != name:
                                    new[nn] = copy.copy(new[nn])
                                nc._adapt(new[nn])
                            else:
                                new[nn] = nc
                    elif s == '_vtable':
                        if not new:
                            new = cls.__dict__.get(s, False)
                        elif cls.__dict__.get(s, False):
                            new.update(cls.__dict__.get(s))
                    elif hasattr(new, 'update'):
                        new.update(cls.__dict__.get(s, {}))
                    else:
                        new.extend(cls.__dict__.get(s, []))
                    nattr[s] = new
                cls = type(name, (cls, parent_class), nattr)

        obj = object.__new__(cls)
        obj.__init__(pool, cr)
        return obj
    createInstance = classmethod(createInstance)

class osv(osv_base, orm.orm):
    #
    # Goal: try to apply inheritancy at the instanciation level and
    #       put objects in the pool var
    #
    def createInstance(cls, pool, module, cr):
        parent_names = getattr(cls, '_inherit', None)
        if parent_names:
            if isinstance(parent_names, (str, unicode)):
                name = cls._name or parent_names
                parent_names = [parent_names]
            else:
                name = cls._name
            if not name:
                raise TypeError('_name is mandatory in case of multiple inheritance')

            for parent_name in ((type(parent_names)==list) and parent_names or [parent_names]):
                parent_class = pool.get(parent_name).__class__
                assert pool.get(parent_name), "parent class %s does not exist in module %s !" % (parent_name, module)
                nattr = {}
                for s in ('_columns', '_defaults', '_inherits', '_constraints', '_sql_constraints', '_virtuals', '_column_stats', '_vtable'):
                    new = copy.copy(getattr(pool.get(parent_name), s))
                    if s == '_columns':
                        new_cols = cls.__dict__.get(s, {})
                        for nn, nc in new_cols.items():
                            if isinstance(nc, orm.fields.inherit):
                                if parent_name != name:
                                    new[nn] = copy.copy(new[nn])
                                nc._adapt(new[nn])
                            else:
                                new[nn] = nc
                    elif s == '_vtable':
                        if not new:
                            new = cls.__dict__.get(s, False)
                        elif cls.__dict__.get(s, False):
                            new.update(cls.__dict__.get(s))
                    elif hasattr(new, 'update'):
                        new.update(cls.__dict__.get(s, {}))
                    else:
                        if s=='_constraints':
                            for c in cls.__dict__.get(s, []):
                                exist = False
                                for c2 in range(len(new)):
                                     #For _constraints, we should check field and methods as well
                                     if new[c2][2]==c[2] and (new[c2][0] == c[0] \
                                            or getattr(new[c2][0],'__name__', True) == \
                                                getattr(c[0],'__name__', False)):
                                        # If new class defines a constraint with
                                        # same function name, we let it override
                                        # the old one.
                                        new[c2] = c
                                        exist = True
                                        break
                                if not exist:
                                    new.append(c)
                        else:
                            new.extend(cls.__dict__.get(s, []))
                    nattr[s] = new
                cls = type(name, (cls, parent_class), nattr)
        obj = object.__new__(cls)
        obj.__init__(pool, cr)
        return obj
    createInstance = classmethod(createInstance)

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:

