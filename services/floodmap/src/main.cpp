// floodmap_service: a small HTTP job service for flood-delineation runs.
//
// Contract: docs/flood_service_design.md ("Flood service API", "Job folder").
// The service queues POSTed jobs, runs an external runner per job (<runner...> <job_dir>) in its own
// process group, and serves job status (status.json, owned by the runner while it runs) and the
// products listed in status.json under <job_dir>/out/. It keeps no state outside its job folders.

#include <crow.h>

#include <spawn.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <ctime>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <mutex>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

extern char** environ;

namespace fs = std::filesystem;
using json_r = crow::json::rvalue;
using json_w = crow::json::wvalue;

static const char* kVersion = "0.1.0";

// ------------------------------------------------------------------------------------------------
// Utilities

static std::mutex g_log_mutex;

static std::string utc_now(const char* fmt = "%Y-%m-%dT%H:%M:%SZ")
{
    std::time_t t = std::time(nullptr);
    std::tm tm{};
    gmtime_r(&t, &tm);
    char buf[64];
    std::strftime(buf, sizeof buf, fmt, &tm);
    return buf;
}

static void log_line(const std::string& s)
{
    std::lock_guard<std::mutex> lk(g_log_mutex);
    std::cout << utc_now() << " " << s << std::endl;
}

static bool read_file(const fs::path& p, std::string& out)
{
    std::ifstream f(p, std::ios::binary);
    if (!f) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    out = ss.str();
    return true;
}

// Atomic write: temp file in the same folder, then rename.
static bool write_file_atomic(const fs::path& p, const std::string& data)
{
    fs::path tmp = p;
    tmp += ".tmp." + std::to_string(::getpid());
    {
        std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
        if (!f) return false;
        f << data;
        if (!f) return false;
    }
    std::error_code ec;
    fs::rename(tmp, p, ec);
    return !ec;
}

// Names used as path components (site, job id, member): letters, digits, '_', '-', '.', no leading '.'.
static bool safe_name(const std::string& s)
{
    if (s.empty() || s.size() > 128 || s[0] == '.') return false;
    for (char c : s)
        if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-' || c == '.')) return false;
    return true;
}

static bool is_under(const fs::path& child, const fs::path& root)
{
    auto r = root.lexically_normal().string();
    auto c = child.lexically_normal().string();
    if (!r.empty() && r.back() != '/') r += '/';
    return c.compare(0, r.size(), r) == 0;
}

static crow::response json_response(int code, const json_w& w)
{
    crow::response res(code);
    res.set_header("Content-Type", "application/json");
    res.body = w.dump();
    return res;
}

static crow::response error_response(int code, const std::string& msg)
{
    json_w w;
    w["error"] = msg;
    return json_response(code, w);
}

static std::string content_type_for(const fs::path& p)
{
    std::string ext = p.extension().string();
    std::transform(ext.begin(), ext.end(), ext.begin(), ::tolower);
    static const std::map<std::string, std::string> types = {
        {".png", "image/png"},        {".tif", "image/tiff"},       {".tiff", "image/tiff"},
        {".json", "application/json"}, {".geojson", "application/geo+json"},
        {".csv", "text/csv; charset=utf-8"}, {".mp4", "video/mp4"},
        {".md", "text/markdown; charset=utf-8"}, {".txt", "text/plain; charset=utf-8"},
        {".log", "text/plain; charset=utf-8"}};
    auto it = types.find(ext);
    return it == types.end() ? "application/octet-stream" : it->second;
}

static bool json_is_object(const json_r& r) { return r && r.t() == crow::json::type::Object; }
static bool json_is_list(const json_r& r) { return r && r.t() == crow::json::type::List; }
static bool json_is_string(const json_r& r) { return r && r.t() == crow::json::type::String; }

static std::string json_str(const json_r& obj, const char* key, const std::string& def = "")
{
    if (json_is_object(obj) && obj.has(key) && json_is_string(obj[key])) return std::string(obj[key].s());
    return def;
}

// ------------------------------------------------------------------------------------------------
// Config

struct Config
{
    std::string host = "0.0.0.0";
    int port = 8300;
    fs::path jobs_dir;
    fs::path sites_dir;
    fs::path workdir;  // runner's working directory
    std::vector<std::string> runner = {"python3", "pipeline/run_job.py"};
    int max_concurrent = 1;
    std::string token;
    bool requeue_on_start = false;
    int kill_grace_s = 10;
};

static fs::path exe_dir()
{
    std::error_code ec;
    fs::path p = fs::read_symlink("/proc/self/exe", ec);
    return ec ? fs::current_path() : p.parent_path();
}

static bool load_config(const fs::path& path, Config& cfg, std::string& err)
{
    std::string text;
    if (!read_file(path, text)) { err = "cannot read config " + path.string(); return false; }
    auto j = crow::json::load(text);
    if (!json_is_object(j)) { err = "config is not a JSON object: " + path.string(); return false; }
    fs::path base = fs::absolute(path).parent_path();
    auto resolve = [&](const std::string& s) { fs::path p(s); return (p.is_absolute() ? p : base / p).lexically_normal(); };

    cfg.host = json_str(j, "host", cfg.host);
    if (j.has("port")) cfg.port = static_cast<int>(j["port"].i());
    cfg.jobs_dir = resolve(json_str(j, "jobs_dir", "jobs"));
    cfg.sites_dir = resolve(json_str(j, "sites_dir", "sites"));
    cfg.workdir = resolve(json_str(j, "workdir", "."));
    if (j.has("runner"))
    {
        if (!json_is_list(j["runner"]) || j["runner"].size() == 0) { err = "runner must be a non-empty list"; return false; }
        cfg.runner.clear();
        for (const auto& a : j["runner"]) cfg.runner.push_back(std::string(a.s()));
    }
    if (j.has("max_concurrent")) cfg.max_concurrent = std::max<int>(1, static_cast<int>(j["max_concurrent"].i()));
    cfg.token = json_str(j, "token", "");
    if (j.has("requeue_on_start")) cfg.requeue_on_start = j["requeue_on_start"].t() == crow::json::type::True;
    if (j.has("kill_grace_s")) cfg.kill_grace_s = std::max<int>(0, static_cast<int>(j["kill_grace_s"].i()));
    return true;
}

// ------------------------------------------------------------------------------------------------
// Job manager

class JobManager
{
public:
    explicit JobManager(const Config& cfg) : cfg_(cfg), rng_(std::random_device{}()) {}

    fs::path job_dir(const std::string& id) const { return cfg_.jobs_dir / id; }
    fs::path status_path(const std::string& id) const { return job_dir(id) / "status.json"; }

    // Read status.json (retries briefly in case a runner writes it non-atomically).
    bool read_status(const std::string& id, json_r& out, std::string* raw = nullptr) const
    {
        for (int k = 0; k < 5; ++k)
        {
            std::string text;
            if (read_file(status_path(id), text))
            {
                auto j = crow::json::load(text);
                if (json_is_object(j)) { out = std::move(j); if (raw) *raw = text; return true; }  // move: a copy would not own the buffer
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(40));
        }
        return false;
    }

    // Update fields of status.json, keeping everything else (products, progress, ...).
    void update_status(const std::string& id, const std::map<std::string, std::string>& fields,
                       bool set_finished = false)
    {
        json_r cur;
        json_w w;
        if (read_status(id, cur)) w = json_w(cur);
        else { w["job_id"] = id; w["products"] = json_w(crow::json::wvalue::object()); }
        for (const auto& kv : fields) w[kv.first] = kv.second;
        if (set_finished) w["finished_utc"] = utc_now();
        write_file_atomic(status_path(id), w.dump());
    }

    // Creates the job folder with request.json and the initial status.json; returns the job id.
    bool create_job(const std::string& site, const std::string& body, const std::string& issued, std::string& id,
                    std::string& err)
    {
        std::error_code ec;
        fs::create_directories(cfg_.jobs_dir, ec);
        std::string stamp = utc_now("%Y%m%dT%H%MZ");
        for (int attempt = 0; attempt < 50; ++attempt)
        {
            char hex[8];
            {
                std::lock_guard<std::mutex> lk(mu_);
                std::snprintf(hex, sizeof hex, "%04x", static_cast<unsigned>(rng_() & 0xffff));
            }
            id = stamp + "_" + site + "_" + hex;
            if (::mkdir(job_dir(id).c_str(), 0755) == 0) break;
            if (errno != EEXIST) { err = std::string("mkdir failed: ") + std::strerror(errno); return false; }
            id.clear();
        }
        if (id.empty()) { err = "could not allocate a job id"; return false; }
        fs::create_directories(job_dir(id) / "out", ec);
        if (!write_file_atomic(job_dir(id) / "request.json", body)) { err = "cannot write request.json"; return false; }
        json_w s;
        s["job_id"] = id;
        s["site"] = site;
        s["state"] = "queued";
        s["progress"] = 0;
        s["message"] = "queued";
        s["created_utc"] = utc_now();
        s["issued_utc"] = issued;
        s["products"] = json_w(crow::json::wvalue::object());
        if (!write_file_atomic(status_path(id), s.dump())) { err = "cannot write status.json"; return false; }
        enqueue(id);
        return true;
    }

    void enqueue(const std::string& id)
    {
        {
            std::lock_guard<std::mutex> lk(mu_);
            queue_.push_back(id);
        }
        cv_.notify_one();
    }

    // Startup: jobs left queued/running by a previous service instance.
    void recover()
    {
        std::error_code ec;
        fs::create_directories(cfg_.jobs_dir, ec);
        std::vector<std::string> ids;
        for (const auto& e : fs::directory_iterator(cfg_.jobs_dir, ec))
            if (e.is_directory() && safe_name(e.path().filename().string())) ids.push_back(e.path().filename().string());
        std::sort(ids.begin(), ids.end());
        for (const auto& id : ids)
        {
            json_r st;
            if (!read_status(id, st)) continue;
            std::string state = json_str(st, "state");
            if (state != "running" && state != "queued") continue;
            if (cfg_.requeue_on_start)
            {
                update_status(id, {{"state", "queued"}, {"message", "requeued after service restart"}});
                enqueue(id);
                log_line("recover: requeued " + id);
            }
            else
            {
                update_status(id, {{"state", "failed"}, {"message", "service restarted"}}, true);
                log_line("recover: marked failed " + id);
            }
        }
    }

    void start_workers()
    {
        for (int i = 0; i < cfg_.max_concurrent; ++i) workers_.emplace_back([this] { worker_loop(); });
    }

    // Stop: no new jobs start; running runners get SIGTERM (SIGKILL after the grace period).
    void shutdown()
    {
        {
            std::lock_guard<std::mutex> lk(mu_);
            stop_ = true;
            for (auto& kv : running_) request_cancel_locked(kv.second, "service stopped");
        }
        cv_.notify_all();
        for (auto& t : workers_) t.join();
        workers_.clear();
    }

    enum class CancelResult { Cancelled, Cancelling, NotActive, NotFound };

    CancelResult cancel(const std::string& id)
    {
        bool was_queued = false;
        {
            std::lock_guard<std::mutex> lk(mu_);
            auto it = std::find(queue_.begin(), queue_.end(), id);
            if (it != queue_.end()) { queue_.erase(it); was_queued = true; }
            else
            {
                auto r = running_.find(id);
                if (r != running_.end()) { request_cancel_locked(r->second, "cancelled"); return CancelResult::Cancelling; }
            }
        }
        if (was_queued)
        {
            update_status(id, {{"state", "failed"}, {"message", "cancelled"}}, true);
            return CancelResult::Cancelled;
        }
        return fs::is_directory(job_dir(id)) ? CancelResult::NotActive : CancelResult::NotFound;
    }

    void counts(size_t& queued, size_t& running) const
    {
        std::lock_guard<std::mutex> lk(mu_);
        queued = queue_.size();
        running = running_.size();
    }

private:
    struct Running
    {
        pid_t pid = 0;
        bool cancel = false;
        bool killed = false;
        std::string reason;
        std::chrono::steady_clock::time_point cancel_at;
    };

    void request_cancel_locked(Running& r, const std::string& reason)
    {
        if (r.cancel) return;
        r.cancel = true;
        r.reason = reason;
        r.cancel_at = std::chrono::steady_clock::now();
        if (r.pid > 0) ::kill(-r.pid, SIGTERM);
    }

    void worker_loop()
    {
        for (;;)
        {
            std::string id;
            {
                std::unique_lock<std::mutex> lk(mu_);
                cv_.wait(lk, [this] { return stop_ || !queue_.empty(); });
                if (stop_) return;
                id = queue_.front();
                queue_.pop_front();
                running_[id] = Running{};
            }
            run_job(id);
            {
                std::lock_guard<std::mutex> lk(mu_);
                running_.erase(id);
            }
        }
    }

    void run_job(const std::string& id)
    {
        const fs::path dir = job_dir(id);
        update_status(id, {{"state", "running"}, {"message", "starting runner"}, {"started_utc", utc_now()}});

        std::vector<std::string> args = cfg_.runner;
        args.push_back(dir.string());
        std::vector<char*> argv;
        for (auto& a : args) argv.push_back(const_cast<char*>(a.c_str()));
        argv.push_back(nullptr);
        const std::string logp = (dir / "runner.log").string();

        posix_spawn_file_actions_t fa;
        posix_spawnattr_t attr;
        posix_spawn_file_actions_init(&fa);
        posix_spawnattr_init(&attr);
        posix_spawn_file_actions_addopen(&fa, 1, logp.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
        posix_spawn_file_actions_adddup2(&fa, 1, 2);
        posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
        posix_spawn_file_actions_addchdir_np(&fa, cfg_.workdir.c_str());
        posix_spawn_file_actions_addclosefrom_np(&fa, 3);  // do not leak the listening socket
        sigset_t none, defs;
        sigemptyset(&none);
        sigemptyset(&defs);
        for (int s : {SIGPIPE, SIGINT, SIGTERM, SIGHUP, SIGQUIT, SIGCHLD}) sigaddset(&defs, s);
        posix_spawnattr_setsigmask(&attr, &none);
        posix_spawnattr_setsigdefault(&attr, &defs);
        posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSID | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);

        pid_t pid = 0;
        int rc = posix_spawnp(&pid, argv[0], &fa, &attr, argv.data(), environ);
        posix_spawn_file_actions_destroy(&fa);
        posix_spawnattr_destroy(&attr);
        if (rc != 0)
        {
            log_line("job " + id + ": spawn failed: " + std::strerror(rc));
            update_status(id, {{"state", "failed"}, {"message", std::string("runner spawn failed: ") + std::strerror(rc)}},
                          true);
            return;
        }
        log_line("job " + id + ": runner started, pid " + std::to_string(pid));
        {
            std::lock_guard<std::mutex> lk(mu_);
            auto& r = running_[id];
            r.pid = pid;
            if (r.cancel) ::kill(-pid, SIGTERM);  // cancel arrived before the pid was known
        }

        int wstatus = 0;
        for (;;)
        {
            pid_t w = ::waitpid(pid, &wstatus, WNOHANG);
            if (w == pid) break;
            if (w < 0 && errno != EINTR) { wstatus = -1; break; }
            {
                std::lock_guard<std::mutex> lk(mu_);
                auto& r = running_[id];
                if (r.cancel && !r.killed &&
                    std::chrono::steady_clock::now() - r.cancel_at > std::chrono::seconds(cfg_.kill_grace_s))
                {
                    ::kill(-pid, SIGKILL);
                    r.killed = true;
                    log_line("job " + id + ": SIGKILL sent to process group " + std::to_string(pid));
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

        bool cancelled;
        std::string reason;
        {
            std::lock_guard<std::mutex> lk(mu_);
            cancelled = running_[id].cancel;
            reason = running_[id].reason;
        }
        if (cancelled) ::kill(-pid, SIGKILL);  // leftovers of the group, if any

        std::string how;
        if (wstatus == -1) how = "runner wait failed";
        else if (WIFEXITED(wstatus)) how = "runner exited with code " + std::to_string(WEXITSTATUS(wstatus));
        else if (WIFSIGNALED(wstatus)) how = "runner killed by signal " + std::to_string(WTERMSIG(wstatus));

        json_r st;
        std::string state = read_status(id, st) ? json_str(st, "state") : "";
        if (cancelled && state != "done" && reason == "service stopped" && cfg_.requeue_on_start)
            update_status(id, {{"state", "queued"}, {"message", "interrupted by service stop; requeued on start"}});
        else if (cancelled && state != "done")
            update_status(id, {{"state", "failed"}, {"message", reason}}, true);
        else if (state != "done")
            update_status(id, {{"state", "failed"}, {"message", how}}, true);
        else if (!st.has("finished_utc"))
            update_status(id, {}, true);
        log_line("job " + id + ": " + how + (cancelled ? " (" + reason + ")" : "") + ", final state " +
                 (state == "done" ? "done" : "failed"));
    }

    const Config& cfg_;
    mutable std::mutex mu_;
    std::condition_variable cv_;
    std::deque<std::string> queue_;
    std::map<std::string, Running> running_;
    std::vector<std::thread> workers_;
    std::mt19937 rng_;
    bool stop_ = false;
};

// ------------------------------------------------------------------------------------------------
// Request validation

static bool validate_job(const json_r& j, const Config& cfg, std::string& site, std::string& err)
{
    if (!json_is_object(j)) { err = "body must be a JSON object"; return false; }
    site = json_str(j, "site");
    if (site.empty()) { err = "missing 'site' (string)"; return false; }
    if (!safe_name(site) || !fs::is_regular_file(cfg.sites_dir / (site + ".json")))
    {
        err = "unknown site '" + site + "'";
        return false;
    }
    if (j.has("issued_utc") && !json_is_string(j["issued_utc"])) { err = "'issued_utc' must be a string"; return false; }
    if (!j.has("members") || !json_is_object(j["members"]) || j["members"].size() == 0)
    {
        err = "'members' must be a non-empty object";
        return false;
    }
    for (const auto& m : j["members"])
    {
        std::string name(m.key());
        if (!safe_name(name)) { err = "invalid member name '" + name + "'"; return false; }
        if (!json_is_object(m)) { err = "member '" + name + "' must be an object"; return false; }
        if (!m.has("t_utc") || !json_is_list(m["t_utc"]) || m["t_utc"].size() == 0)
        {
            err = "member '" + name + "': 't_utc' must be a non-empty array";
            return false;
        }
        size_t n = m["t_utc"].size();
        for (const auto& t : m["t_utc"])
            if (!json_is_string(t)) { err = "member '" + name + "': 't_utc' entries must be strings"; return false; }
        if (!m.has("links") || !json_is_object(m["links"]) || m["links"].size() == 0)
        {
            err = "member '" + name + "': 'links' must be a non-empty object";
            return false;
        }
        for (const auto& l : m["links"])
        {
            std::string ln(l.key());
            if (!json_is_list(l) || l.size() != n)
            {
                err = "member '" + name + "', link '" + ln + "': must be an array of the same length as t_utc (" +
                      std::to_string(n) + ")";
                return false;
            }
            for (const auto& v : l)
                if (v.t() != crow::json::type::Number)
                {
                    err = "member '" + name + "', link '" + ln + "': values must be numbers";
                    return false;
                }
        }
    }
    return true;
}

static void collect_strings(const json_r& r, std::set<std::string>& out)
{
    if (!r) return;
    if (r.t() == crow::json::type::String) out.insert(std::string(r.s()));
    else if (r.t() == crow::json::type::Object || r.t() == crow::json::type::List)
        for (const auto& c : r) collect_strings(c, out);
}

// ------------------------------------------------------------------------------------------------
// Request log middleware

struct RequestLog
{
    struct context
    {
        std::chrono::steady_clock::time_point t0;
    };
    void before_handle(crow::request&, crow::response&, context& ctx) { ctx.t0 = std::chrono::steady_clock::now(); }
    void after_handle(crow::request& req, crow::response& res, context& ctx)
    {
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - ctx.t0).count();
        log_line(req.remote_ip_address + " " + crow::method_name(req.method) + " " + req.raw_url + " " +
                 std::to_string(res.code) + " " + std::to_string(ms) + "ms");
    }
};

// ------------------------------------------------------------------------------------------------

static void usage()
{
    std::cerr << "usage: floodmap_service [--config service.json]\n"
                 "  default config: service.json next to the binary\n";
}

int main(int argc, char** argv)
{
    fs::path cfg_path = exe_dir() / "service.json";
    for (int i = 1; i < argc; ++i)
    {
        std::string a = argv[i];
        if (a == "--config" && i + 1 < argc) cfg_path = argv[++i];
        else if (a == "--help" || a == "-h") { usage(); return 0; }
        else { usage(); return 2; }
    }
    Config cfg;
    std::string err;
    if (!load_config(cfg_path, cfg, err)) { std::cerr << "floodmap_service: " << err << "\n"; return 1; }
    ::signal(SIGPIPE, SIG_IGN);

    log_line(std::string("floodmap_service ") + kVersion + " config " + fs::absolute(cfg_path).string());
    log_line("jobs_dir " + cfg.jobs_dir.string() + ", sites_dir " + cfg.sites_dir.string() + ", workdir " +
             cfg.workdir.string() + ", max_concurrent " + std::to_string(cfg.max_concurrent) +
             (cfg.token.empty() ? ", no token" : ", token required for POST/DELETE"));

    JobManager jobs(cfg);
    jobs.recover();
    jobs.start_workers();

    auto authorized = [&cfg](const crow::request& req) {
        if (cfg.token.empty()) return true;
        const std::string got = req.get_header_value("X-Floodmap-Token");
        if (got.size() != cfg.token.size()) return false;
        unsigned char diff = 0;
        for (size_t i = 0; i < got.size(); ++i) diff |= static_cast<unsigned char>(got[i] ^ cfg.token[i]);
        return diff == 0;
    };

    crow::App<RequestLog> app;
    app.loglevel(crow::LogLevel::Warning);

    CROW_ROUTE(app, "/health")
    ([&jobs] {
        size_t q, r;
        jobs.counts(q, r);
        json_w w;
        w["ok"] = true;
        w["version"] = kVersion;
        w["queued"] = q;
        w["running"] = r;
        return json_response(200, w);
    });

    CROW_ROUTE(app, "/sites")
    ([&cfg] {
        json_w sites(crow::json::wvalue::object{});
        std::error_code ec;
        std::vector<fs::path> files;
        for (const auto& e : fs::directory_iterator(cfg.sites_dir, ec))
            if (e.is_regular_file() && e.path().extension() == ".json") files.push_back(e.path());
        std::sort(files.begin(), files.end());
        for (const auto& p : files)
        {
            std::string name = p.stem().string();
            if (!safe_name(name)) continue;
            std::string text;
            json_w s;
            s["label"] = nullptr;
            s["grid_m"] = nullptr;
            if (read_file(p, text))
            {
                auto j = crow::json::load(text);
                if (json_is_object(j))
                {
                    if (j.has("label")) s["label"] = json_w(j["label"]);
                    if (j.has("grid_m")) s["grid_m"] = json_w(j["grid_m"]);
                }
                else s["error"] = "invalid JSON";
            }
            sites[name] = std::move(s);
        }
        json_w w;
        w["sites"] = std::move(sites);
        return json_response(200, w);
    });

    CROW_ROUTE(app, "/jobs").methods(crow::HTTPMethod::Get, crow::HTTPMethod::Post)
    ([&](const crow::request& req) {
        if (req.method == crow::HTTPMethod::Post)
        {
            if (!authorized(req)) return error_response(401, "missing or wrong X-Floodmap-Token");
            auto j = crow::json::load(req.body);
            if (!j) return error_response(400, "body is not valid JSON");
            std::string site, verr;
            if (!validate_job(j, cfg, site, verr)) return error_response(400, verr);
            std::string id, cerr;
            if (!jobs.create_job(site, req.body, json_str(j, "issued_utc"), id, cerr))
                return error_response(500, cerr);
            log_line("job " + id + ": queued (requester " + json_str(j, "requester", "-") + ", " +
                     std::to_string(j["members"].size()) + " members)");
            json_w w;
            w["job_id"] = id;
            w["state"] = "queued";
            return json_response(202, w);
        }
        // GET: list job folders, newest first; optional ?site= and ?state= filters.
        const char* fsite = req.url_params.get("site");
        const char* fstate = req.url_params.get("state");
        std::vector<std::string> ids;
        std::error_code ec;
        for (const auto& e : fs::directory_iterator(cfg.jobs_dir, ec))
            if (e.is_directory() && safe_name(e.path().filename().string())) ids.push_back(e.path().filename().string());
        std::sort(ids.rbegin(), ids.rend());
        crow::json::wvalue::list list;
        for (const auto& id : ids)
        {
            json_r st;
            if (!jobs.read_status(id, st)) continue;
            std::string site = json_str(st, "site");
            std::string state = json_str(st, "state");
            if (fsite && site != fsite) continue;
            if (fstate && state != fstate) continue;
            json_w e;
            e["job_id"] = id;
            e["site"] = site;
            e["state"] = state;
            e["progress"] = st.has("progress") ? json_w(st["progress"]) : json_w(nullptr);
            e["message"] = json_str(st, "message");
            for (const char* k : {"created_utc", "issued_utc", "started_utc", "finished_utc"})
                e[k] = json_str(st, k);
            list.push_back(std::move(e));
        }
        json_w w;
        w["jobs"] = std::move(list);
        return json_response(200, w);
    });

    CROW_ROUTE(app, "/jobs/<string>").methods(crow::HTTPMethod::Get, crow::HTTPMethod::Delete)
    ([&](const crow::request& req, const std::string& id) {
        if (!safe_name(id) || !fs::is_directory(jobs.job_dir(id))) return error_response(404, "no such job");
        if (req.method == crow::HTTPMethod::Delete)
        {
            if (!authorized(req)) return error_response(401, "missing or wrong X-Floodmap-Token");
            json_w w;
            w["job_id"] = id;
            switch (jobs.cancel(id))
            {
            case JobManager::CancelResult::Cancelled:
                w["state"] = "failed";
                w["message"] = "cancelled";
                return json_response(200, w);
            case JobManager::CancelResult::Cancelling:
                w["state"] = "cancelling";
                w["message"] = "SIGTERM sent to the runner's process group";
                return json_response(202, w);
            case JobManager::CancelResult::NotActive:
            {
                json_r st;
                w["state"] = jobs.read_status(id, st) ? json_str(st, "state") : "";
                w["error"] = "job is not queued or running";
                return json_response(409, w);
            }
            default: return error_response(404, "no such job");
            }
        }
        json_r st;
        std::string raw;
        if (!jobs.read_status(id, st, &raw)) return error_response(500, "status.json missing or unreadable");
        crow::response res(200);
        res.set_header("Content-Type", "application/json");
        res.body = raw;
        return res;
    });

    CROW_ROUTE(app, "/jobs/<string>/files/<path>")
    ([&](const std::string& id, const std::string& rel) {
        if (!safe_name(id) || !fs::is_directory(jobs.job_dir(id))) return error_response(404, "no such job");
        json_r st;
        if (!jobs.read_status(id, st)) return error_response(404, "no status");
        std::set<std::string> products;
        if (st.has("products")) collect_strings(st["products"], products);
        if (!products.count(rel)) return error_response(404, "not a listed product");
        std::error_code ec;
        fs::path out_root = fs::canonical(jobs.job_dir(id) / "out", ec);
        if (ec) return error_response(404, "no out/ folder");
        fs::path target = fs::canonical(out_root / fs::path(rel), ec);  // resolves symlinks and '..'
        if (ec || !is_under(target, out_root) || !fs::is_regular_file(target))
            return error_response(404, "product not available");
        crow::response res;
        res.set_static_file_info_unsafe(target.string());
        if (res.code != 200) return error_response(404, "product not available");
        res.set_header("Content-Type", content_type_for(target));
        return res;
    });

    app.bindaddr(cfg.host).port(static_cast<uint16_t>(cfg.port)).multithreaded();
    log_line("listening on " + cfg.host + ":" + std::to_string(cfg.port));
    app.run();

    log_line("stopping: cancelling running jobs");
    jobs.shutdown();
    log_line("stopped");
    return 0;
}
