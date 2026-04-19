
@app.route("/api/admin/press-releases", methods=["POST"])
def create_press_release():
    auth = request.headers.get("Authorization", "")
    if auth.replace("Bearer ", "").strip() != os.getenv("DCHUB_ADMIN_API_KEY"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    return jsonify({"id": 1, "slug": data.get("slug")}), 201
