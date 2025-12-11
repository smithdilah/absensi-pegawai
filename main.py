import os
from datetime import datetime, timedelta
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_file, flash
)
from supabase import create_client
import pandas as pd
from dotenv import load_dotenv

# ---------------- Load .env ----------------
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase URL/KEY not found. Set SUPABASE_URL and SUPABASE_KEY in .env")

app = Flask(__name__)
app.secret_key = SECRET_KEY
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------- Helper (safe wrapper for supabase calls) ----------------
def safe_execute(callable_fn, *args, **kwargs):
    """
    Jalankan callable_fn yang memanggil supabase (mis. supabase.table(...).select(...).execute())
    Kembalikan tuple (ok: bool, result_or_error)
    - ok True => result_or_error adalah objek response yang memiliki .data (atau list)
    - ok False => result_or_error adalah string pesan error untuk ditampilkan/di-log
    """
    try:
        res = callable_fn(*args, **kwargs)
    except Exception as e:
        # tangkap semua exception (termasuk postgrest.exceptions.APIError)
        msg = f"Supabase request failed: {repr(e)}"
        print(msg)
        return False, msg

    # beberapa client mengembalikan object dengan .data; pastikan ada
    if res is None:
        msg = "Supabase returned None response"
        print(msg)
        return False, msg

    # Jika object memiliki .data -> gunakan itu
    data = getattr(res, "data", None)
    # Be tolerant: beberapa wrapper bisa mengembalikan dict dengan 'data' key
    if data is None and isinstance(res, dict) and "data" in res:
        data = res["data"]

    # Jika masih None, kembalikan error message (mungkin HTML/500)
    if data is None:
        # coba ambil message / status_code / content jika ada
        extra = {}
        for attr in ("status_code", "message", "error", "content", "text"):
            if hasattr(res, attr):
                extra[attr] = getattr(res, attr)
        msg = f"Supabase returned unexpected format. Extra: {extra}"
        print(msg)
        return False, msg

    return True, res

def get_users_by_ids(id_list):
    if not id_list:
        return {}
    ok, res = safe_execute(lambda: supabase.table("users").select("id,nama").in_("id", id_list).execute())
    if not ok:
        # log & kembalikan mapping kosong supaya aplikasi tidak crash
        print("get_users_by_ids error:", res)
        return {}
    return {u["id"]: u.get("nama", "") for u in (res.data or [])}

def require_login():
    return session.get("user_id") is not None

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        ok, res = safe_execute(
            lambda: supabase.table("users")
            .select("*")
            .eq("username", username)
            .eq("password", password)
            .execute()
        )
        user = res.data[0] if ok and res.data else None

        if not user:
            flash("Username atau password salah")
            return redirect(url_for("login"))

        # Simpan session
        session["user_id"] = user["id"]    
        session["id"] = user["id"]      
        session["username"] = user["username"]
        session["role"] = user["role"]            # admin / pegawai
        session["unit_id"] = user.get("unit_id")
        session["id_pegawai"] = user.get("id_pegawai")  # INT (FK pegawai)

        today = datetime.now().strftime("%Y-%m-%d")

        # ================================
        #  CEK DANTON MENGGUNAKAN 2 SKEMA
        # ================================

        # Skema 1 → jadwal_danton.danton_id = users.id (UUID)
        ok1, res1 = safe_execute(
            lambda: supabase.table("jadwal_danton")
            .select("*")
            .eq("tanggal", today)
            .eq("danton_id", user["id"])
            .execute()
        )

        # Skema 2 → jadwal_danton.danton_id = pegawai.id (INT)
        ok2, res2 = safe_execute(
            lambda: supabase.table("jadwal_danton")
            .select("*")
            .eq("tanggal", today)
            .eq("danton_id", user.get("id_pegawai"))
            .execute()
        )

        # ================================
        #  MENENTUKAN DANTON
        # ================================
        jadwal = None
        if ok1 and res1.data:
            jadwal = res1.data[0]
        elif ok2 and res2.data:
            jadwal = res2.data[0]

        if jadwal:
            session["is_danton_today"] = True
            session["danton_unit_id"] = jadwal["unit_id"]
            flash("Anda ditugaskan sebagai Danton hari ini.")
            return redirect(url_for("dashboard_danton"))

 
        if user["role"] == "admin":
            return redirect(url_for("dashboard_admin"))
        else:
            return redirect(url_for("dashboard_pegawai"))

    return render_template("login.html")

# ==================== DASHBOARD ADMIN ====================
@app.route('/dashboard_admin')
def dashboard_admin():
    if 'role' not in session or session['role'] != 'admin':
        flash("Akses hanya untuk admin")
        return redirect(url_for('login'))
    return render_template('dashboard_admin.html')


# ==================== DASHBOARD DANTON ====================
@app.route('/dashboard_danton')
def dashboard_danton():
    if not session.get("is_danton_today"):
        flash("Anda tidak dijadwalkan sebagai danton hari ini")
        return redirect(url_for('login'))
    return render_template('dashboard_danton.html')


# ==================== DASHBOARD PEGAWAI ====================
@app.route('/dashboard_pegawai')
def dashboard_pegawai():
    # Pegawai biasa, tapi tidak danton hari ini
    if 'role' not in session or session['role'] != 'pegawai' or session.get("is_danton_today"):
        flash("Akses hanya untuk pegawai biasa")
        return redirect(url_for('login'))
    return render_template('dashboard_pegawai.html')


# ==================== ABSEN DANTON ====================
@app.route("/absen-danton", methods=["GET", "POST"])
def absen_danton():
    tanggal = datetime.now().strftime("%Y-%m-%d")

    # pastikan session unit ada
    unit_id = session.get("unit_id")
    if not unit_id:
        flash("Unit tidak ditemukan. Silakan login ulang.")
        return redirect("/login")

    # ambil pegawai berdasarkan unit
    pegawai = supabase.table("users") \
        .select("*") \
        .eq("role", "pegawai") \
        .eq("unit_id", unit_id) \
        .execute().data

    if request.method == "POST":
        for p in pegawai:

            # PERBAIKAN DI SINI  ↓↓↓
            status = request.form.get(f"status_{p['id']}")
            keterangan = request.form.get(f"keterangan_{p['id']}", "")

            if not status:
                print("DEBUG: Status kosong untuk pegawai:", p["id"])
                continue

            supabase.table("absensi").insert({
                "pegawai_id": p["id"],
                "danton_id": session.get("id"),
                "tanggal": request.form.get("tanggal"),
                "status": status,
                "keterangan": keterangan,
                "unit_id" : unit_id

            }).execute()

        flash("Absensi berhasil disimpan")
        return redirect("/dashboard_danton")

    return render_template("absen_danton.html", pegawai=pegawai, tanggal=tanggal)


# ==================== KELOLA JADWAL ====================
@app.route("/kelola_jadwal", methods=["GET", "POST"])
def kelola_jadwal():
    if 'role' not in session or session['role'] != 'admin':
        return redirect(url_for('login'))

    # ------------ HAPUS JADWAL LAMA (lebih dari 24 jam) ------------
    now = datetime.now()
    ok_old, res_old = safe_execute(
        lambda: supabase.table("jadwal_danton").select("*").execute()
    )
    if ok_old and res_old.data:
        for j in res_old.data:
            jadwal_tanggal = datetime.strptime(j["tanggal"], "%Y-%m-%d")
            if now - jadwal_tanggal > timedelta(hours=24):
                safe_execute(
                    lambda: supabase.table("jadwal_danton").delete().eq("id", j["id"]).execute()
                )

    # ------------ SIMPAN JADWAL ------------
    if request.method == "POST":
        danton_id = request.form.get("danton_id")
        unit_id = request.form.get("unit_id")
        tanggal = request.form.get("tanggal")
        keterangan = request.form.get("keterangan")

        safe_execute(
            lambda: supabase.table("jadwal_danton").insert({
                "danton_id": danton_id,
                "unit_id": unit_id,
                "tanggal": tanggal,
                "keterangan": keterangan
            }).execute()
        )

        flash("Jadwal berhasil disimpan.")

    # ------------ AMBIL UNIT ------------
    ok_u, res_u = safe_execute(
        lambda: supabase.table("unit").select("*").order("id").execute()
    )
    unit = res_u.data if ok_u else []

    # ------------ AMBIL PEGAWAI ------------
    ok_p, res_p = safe_execute(
        lambda: supabase.table("pegawai").select("id,nama,unit").order("nama").execute()
    )
    pegawai = res_p.data if ok_p else []

    pegawai_json = [
        {
            "id": p["id"],
            "nama": p["nama"],
            "unit_nama": p["unit"]
        }
        for p in pegawai
    ]

    # ------------ AMBIL DATA JADWAL ------------
    ok_j, res_j = safe_execute(
        lambda: supabase.table("jadwal_danton").select("*").order("tanggal", desc=True).execute()
    )
    jadwal_raw = res_j.data if ok_j else []

    pegawai_map = {p["id"]: p["nama"] for p in pegawai}
    unit_map = {u["id"]: u["nama"] for u in unit}

    jadwal = [
        {
            "nama_danton": pegawai_map.get(j["danton_id"], "??"),
            "unit": unit_map.get(j["unit_id"], "??"),
            "tanggal": j["tanggal"],
            "keterangan": j.get("keterangan", "")
        }
        for j in jadwal_raw
    ]

    return render_template(
        "kelola_jadwal.html",
        unit=unit,
        pegawai_json=pegawai_json,
        jadwal=jadwal
    )


# ---------------- REKAP ABSENSI (ADMIN) ----------------
@app.route("/rekap_absensi_pegawai")
def rekap_absensi_all():
    if not require_login() or session.get("role") != "admin":
        return "Unauthorized", 403

    # Ambil semua absensi
    absensi = supabase.table("absensi").select("*").order("tanggal", desc=True).execute().data or []

    # Ambil semua user (pegawai + danton)
    users = supabase.table("users").select("*").execute().data or []

    # Map ID -> nama dari tabel users (karena pegawai_id masih integer dan cocoknya ke users.id)
    def ambil_nama(u):
        return (
            u.get("nama") or u.get("nama_lengkap") or 
            u.get("full_name") or u.get("username") or 
            f"ID {u.get('id')}"
        )

    users_map = {u["id"]: ambil_nama(u) for u in users}

    # Gabungkan hasil
    enriched = []
    for a in absensi:
        enriched.append({
            "tanggal": a["tanggal"],
            "status": a["status"],
            "nama_pegawai": users_map.get(a.get("pegawai_id"), f"ID {a.get('pegawai_id')}"),
            "nama_danton": users_map.get(a.get("danton_id"), f"ID {a.get('danton_id')}")
        })

    return render_template("rekap_absensi_pegawai.html", absensi=enriched)

# ---------------- REKAP SAYA (PEGAWAI) ----------------
@app.route("/rekap_pegawai")
def rekap_saya():
    if not require_login() or session.get("role") != "pegawai":
        return "Unauthorized", 403

    uid = session.get("user_id")

    absensi = (
        supabase.table("absensi")
        .select("*")
        .eq("pegawai_id", uid)
        .order("tanggal", desc=True)
        .execute()
        .data or []
    )

    users_data = supabase.table("users").select("*").execute().data or []
    danton_map = {
        u["id"]: (
            u.get("nama") or
            u.get("nama_lengkap") or
            u.get("full_name") or
            u.get("username") or
            f"ID {u['id']}"
        )
        for u in users_data
    }

    enriched = []
    for a in absensi:
        enriched.append({
            "tanggal": a["tanggal"],
            "status": a["status"],
            "nama_danton": danton_map.get(a["danton_id"], f"ID {a['danton_id']}")
        })

    return render_template("rekap_pegawai.html", data=enriched)

# ---------------- EXPORT EXCEL (ADMIN) ----------------
@app.route("/export_excel")
def export_excel():
    if not require_login() or session.get("role") != "admin":
        return "Unauthorized", 403

    resp = supabase.table("absensi").select("*").order("tanggal", desc=True).execute()
    absensi = resp.data or []

    if not absensi:
        return "Tidak ada data absensi", 200

    peg_ids = list({a["pegawai_id"] for a in absensi})
    danton_ids = list({a["danton_id"] for a in absensi})
    users_map = get_users_by_ids(peg_ids + danton_ids)

    rows = []
    for a in absensi:
        rows.append({
            "ID": a["id"],
            "Pegawai": users_map.get(a["pegawai_id"], a["pegawai_id"]),
            "Danton": users_map.get(a["danton_id"], a["danton_id"]),
            "Tanggal": a["tanggal"],
            "Status": a["status"]
        })

    df = pd.DataFrame(rows)
    output = BytesIO()
    df.to_excel(output, index=False, engine="openpyxl")
    output.seek(0)
    return send_file(output, download_name="rekap_absensi.xlsx", as_attachment=True)

# ----------------------------------------
# LOGOUT
# ----------------------------------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)
