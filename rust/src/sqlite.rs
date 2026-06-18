use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::path::Path;
use std::ptr;

#[repr(C)]
struct sqlite3 {
    _private: [u8; 0],
}

#[repr(C)]
struct sqlite3_stmt {
    _private: [u8; 0],
}

const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_DONE: c_int = 101;

#[cfg(windows)]
#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open(filename: *const c_char, pp_db: *mut *mut sqlite3) -> c_int;
    fn sqlite3_close(db: *mut sqlite3) -> c_int;
    fn sqlite3_exec(
        db: *mut sqlite3,
        sql: *const c_char,
        callback: Option<
            unsafe extern "C" fn(*mut c_void, c_int, *mut *mut c_char, *mut *mut c_char) -> c_int,
        >,
        arg: *mut c_void,
        errmsg: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_errmsg(db: *mut sqlite3) -> *const c_char;
    fn sqlite3_prepare_v2(
        db: *mut sqlite3,
        sql: *const c_char,
        n_byte: c_int,
        pp_stmt: *mut *mut sqlite3_stmt,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_finalize(stmt: *mut sqlite3_stmt) -> c_int;
    fn sqlite3_step(stmt: *mut sqlite3_stmt) -> c_int;
    fn sqlite3_bind_text(
        stmt: *mut sqlite3_stmt,
        index: c_int,
        value: *const c_char,
        n: c_int,
        destructor: Option<unsafe extern "C" fn(*mut c_void)>,
    ) -> c_int;
    fn sqlite3_bind_int64(stmt: *mut sqlite3_stmt, index: c_int, value: i64) -> c_int;
    fn sqlite3_bind_double(stmt: *mut sqlite3_stmt, index: c_int, value: f64) -> c_int;
    fn sqlite3_bind_null(stmt: *mut sqlite3_stmt, index: c_int) -> c_int;
    fn sqlite3_column_count(stmt: *mut sqlite3_stmt) -> c_int;
    fn sqlite3_column_name(stmt: *mut sqlite3_stmt, index: c_int) -> *const c_char;
    fn sqlite3_column_text(stmt: *mut sqlite3_stmt, index: c_int) -> *const c_char;
}

#[cfg(unix)]
#[link(name = "sqlite3")]
unsafe extern "C" {
    fn sqlite3_open(filename: *const c_char, pp_db: *mut *mut sqlite3) -> c_int;
    fn sqlite3_close(db: *mut sqlite3) -> c_int;
    fn sqlite3_exec(
        db: *mut sqlite3,
        sql: *const c_char,
        callback: Option<
            unsafe extern "C" fn(*mut c_void, c_int, *mut *mut c_char, *mut *mut c_char) -> c_int,
        >,
        arg: *mut c_void,
        errmsg: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_errmsg(db: *mut sqlite3) -> *const c_char;
    fn sqlite3_prepare_v2(
        db: *mut sqlite3,
        sql: *const c_char,
        n_byte: c_int,
        pp_stmt: *mut *mut sqlite3_stmt,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_finalize(stmt: *mut sqlite3_stmt) -> c_int;
    fn sqlite3_step(stmt: *mut sqlite3_stmt) -> c_int;
    fn sqlite3_bind_text(
        stmt: *mut sqlite3_stmt,
        index: c_int,
        value: *const c_char,
        n: c_int,
        destructor: Option<unsafe extern "C" fn(*mut c_void)>,
    ) -> c_int;
    fn sqlite3_bind_int64(stmt: *mut sqlite3_stmt, index: c_int, value: i64) -> c_int;
    fn sqlite3_bind_double(stmt: *mut sqlite3_stmt, index: c_int, value: f64) -> c_int;
    fn sqlite3_bind_null(stmt: *mut sqlite3_stmt, index: c_int) -> c_int;
    fn sqlite3_column_count(stmt: *mut sqlite3_stmt) -> c_int;
    fn sqlite3_column_name(stmt: *mut sqlite3_stmt, index: c_int) -> *const c_char;
    fn sqlite3_column_text(stmt: *mut sqlite3_stmt, index: c_int) -> *const c_char;
}

pub struct Database {
    raw: *mut sqlite3,
}

pub enum Bind {
    Text(String),
    Int(i64),
    Float(f64),
    Null,
}

impl Database {
    pub fn open(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|err| err.to_string())?;
        }
        let path =
            CString::new(path.to_string_lossy().as_bytes()).map_err(|err| err.to_string())?;
        let mut raw = ptr::null_mut();
        let rc = unsafe { sqlite3_open(path.as_ptr(), &mut raw) };
        if rc != SQLITE_OK {
            let message = unsafe { error_message(raw) };
            if !raw.is_null() {
                unsafe {
                    sqlite3_close(raw);
                }
            }
            return Err(message);
        }
        Ok(Self { raw })
    }

    pub fn exec(&self, sql: &str) -> Result<(), String> {
        let sql = CString::new(sql).map_err(|err| err.to_string())?;
        let rc = unsafe {
            sqlite3_exec(
                self.raw,
                sql.as_ptr(),
                None,
                ptr::null_mut(),
                ptr::null_mut(),
            )
        };
        if rc == SQLITE_OK {
            Ok(())
        } else {
            Err(unsafe { error_message(self.raw) })
        }
    }

    pub fn execute(&self, sql: &str, params: &[Bind]) -> Result<(), String> {
        let mut stmt = self.prepare(sql)?;
        stmt.bind_all(params)?;
        stmt.step_done()
    }

    pub fn query(
        &self,
        sql: &str,
        params: &[Bind],
    ) -> Result<Vec<std::collections::BTreeMap<String, String>>, String> {
        let mut stmt = self.prepare(sql)?;
        stmt.bind_all(params)?;
        stmt.query_all()
    }

    fn prepare(&self, sql: &str) -> Result<Statement<'_>, String> {
        let sql = CString::new(sql).map_err(|err| err.to_string())?;
        let mut stmt = ptr::null_mut();
        let rc =
            unsafe { sqlite3_prepare_v2(self.raw, sql.as_ptr(), -1, &mut stmt, ptr::null_mut()) };
        if rc == SQLITE_OK {
            Ok(Statement {
                db: self,
                raw: stmt,
                text_params: Vec::new(),
            })
        } else {
            Err(unsafe { error_message(self.raw) })
        }
    }
}

impl Drop for Database {
    fn drop(&mut self) {
        if !self.raw.is_null() {
            unsafe {
                sqlite3_close(self.raw);
            }
        }
    }
}

struct Statement<'a> {
    db: &'a Database,
    raw: *mut sqlite3_stmt,
    text_params: Vec<CString>,
}

impl Statement<'_> {
    fn bind_all(&mut self, params: &[Bind]) -> Result<(), String> {
        for (index, value) in params.iter().enumerate() {
            let position = (index + 1) as c_int;
            let rc = match value {
                Bind::Text(text) => {
                    let ctext = CString::new(text.as_bytes()).map_err(|err| err.to_string())?;
                    let rc =
                        unsafe { sqlite3_bind_text(self.raw, position, ctext.as_ptr(), -1, None) };
                    self.text_params.push(ctext);
                    rc
                }
                Bind::Int(value) => unsafe { sqlite3_bind_int64(self.raw, position, *value) },
                Bind::Float(value) => unsafe { sqlite3_bind_double(self.raw, position, *value) },
                Bind::Null => unsafe { sqlite3_bind_null(self.raw, position) },
            };
            if rc != SQLITE_OK {
                return Err(unsafe { error_message(self.db.raw) });
            }
        }
        Ok(())
    }

    fn step_done(&mut self) -> Result<(), String> {
        let rc = unsafe { sqlite3_step(self.raw) };
        if rc == SQLITE_DONE || rc == SQLITE_ROW {
            Ok(())
        } else {
            Err(unsafe { error_message(self.db.raw) })
        }
    }

    fn query_all(&mut self) -> Result<Vec<std::collections::BTreeMap<String, String>>, String> {
        let mut rows = Vec::new();
        loop {
            let rc = unsafe { sqlite3_step(self.raw) };
            if rc == SQLITE_DONE {
                break;
            }
            if rc != SQLITE_ROW {
                return Err(unsafe { error_message(self.db.raw) });
            }
            let count = unsafe { sqlite3_column_count(self.raw) };
            let mut row = std::collections::BTreeMap::new();
            for index in 0..count {
                let name_ptr = unsafe { sqlite3_column_name(self.raw, index) };
                let value_ptr = unsafe { sqlite3_column_text(self.raw, index) };
                let name = unsafe { cstr_to_string(name_ptr) };
                let value = unsafe { cstr_to_string(value_ptr) };
                row.insert(name, value);
            }
            rows.push(row);
        }
        Ok(rows)
    }
}

impl Drop for Statement<'_> {
    fn drop(&mut self) {
        if !self.raw.is_null() {
            unsafe {
                sqlite3_finalize(self.raw);
            }
        }
    }
}

unsafe fn error_message(db: *mut sqlite3) -> String {
    if db.is_null() {
        return "sqlite error".to_string();
    }
    unsafe { cstr_to_string(sqlite3_errmsg(db)) }
}

unsafe fn cstr_to_string(ptr: *const c_char) -> String {
    if ptr.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(ptr) }.to_string_lossy().to_string()
    }
}
