(function () {
    'use strict';

    var STORAGE_LANG = 'db-lang';
    var STORAGE_THEME = 'db-theme';
    var STORAGE_FONT = 'db-font';
    var DEFAULT_VIEW = 'mavlink';
    var DEFAULT_THEME = 'light';

    var LEGACY_THEMES = {
        ocean: 'sky', forest: 'sand', purple: 'graphite', carbon: 'midnight', neon: 'graphite', indigo: 'graphite'
    };

    var I18N = {
        vi: {
            brandSub: 'Trung tâm điều khiển PX4',
            collapse: 'Thu gọn',
            language: 'Ngôn ngữ',
            sections: {
                monitor: 'Giám sát',
                control: 'Điều khiển',
                connect: 'Kết nối',
                device: 'Thiết bị',
                security: 'Bảo mật',
                system: 'Hệ thống'
            },
            nav: {
                mavlink: 'MAVLink Monitor',
                params: 'Tham số PX4',
                'mavlink-conn': 'MAVLink / Pixhawk',
                network: 'Server & Mạng',
                hardware: 'Cài đặt phần cứng',
                tokens: 'Token & Phiên',
                settings: 'Cài đặt'
            },
            settings: {
                title: 'Cài đặt',
                desc: 'Tùy chỉnh giao diện và ngôn ngữ hệ thống',
                appearance: 'Giao diện',
                themeHint: 'Chọn bảng màu cho toàn bộ dashboard',
                fontSize: 'Cỡ chữ',
                fontSm: 'Nhỏ',
                fontMd: 'Vừa',
                fontLg: 'Lớn',
                system: 'Hệ thống',
                systemHint: 'Cài đặt phần cứng (camera, motor, GPS…) nằm tại menu Thiết bị → Cài đặt phần cứng.'
            },
            status: {
                system: 'Hệ thống', px4: 'PX4', auth: 'Xác thực',
                network: 'Mạng', mavlink: 'MAVLink', ip: 'IP', uptime: 'Uptime'
            },
            val: {
                online: 'Online', offline: 'Offline', idle: 'Idle',
                heartbeat: 'Heartbeat', connected: 'Connected',
                unknown: 'Unknown', reconnecting: 'Reconnecting...'
            },
            themes: {
                light: 'Sáng', sky: 'Xanh trời', sand: 'Ấm áp',
                midnight: 'Ban đêm', graphite: 'Than chì'
            }
        },
        en: {
            brandSub: 'PX4 Control Center',
            collapse: 'Collapse',
            language: 'Language',
            sections: {
                monitor: 'Monitoring', control: 'Control', connect: 'Connectivity',
                device: 'Device', security: 'Security', system: 'System'
            },
            nav: {
                mavlink: 'MAVLink Monitor', params: 'PX4 Parameters',
                'mavlink-conn': 'MAVLink / Pixhawk', network: 'Server & Network',
                hardware: 'Hardware Setup', tokens: 'Tokens & Sessions',
                settings: 'Settings'
            },
            settings: {
                title: 'Settings',
                desc: 'Customize appearance and language',
                appearance: 'Appearance',
                themeHint: 'Choose a color theme for the entire dashboard',
                fontSize: 'Font size',
                fontSm: 'Small', fontMd: 'Medium', fontLg: 'Large',
                system: 'System',
                systemHint: 'Hardware settings (camera, motors, GPS…) are under Device → Hardware Setup.'
            },
            status: {
                system: 'System', px4: 'PX4', auth: 'Auth',
                network: 'Network', mavlink: 'MAVLink', ip: 'IP', uptime: 'Uptime'
            },
            val: {
                online: 'Online', offline: 'Offline', idle: 'Idle',
                heartbeat: 'Heartbeat', connected: 'Connected',
                unknown: 'Unknown', reconnecting: 'Reconnecting...'
            },
            themes: {
                light: 'Light', sky: 'Sky', sand: 'Warm',
                midnight: 'Midnight', graphite: 'Graphite'
            }
        }
    };

    var NAV_STRUCTURE = [
        { sectionKey: 'monitor', items: [
            { id: 'mavlink', icon: 'radio', src: 'mavlink.html' }
        ]},
        { sectionKey: 'control', items: [
            { id: 'params', icon: 'sliders', src: 'params.html' }
        ]},
        { sectionKey: 'connect', items: [
            { id: 'mavlink-conn', icon: 'plug', src: 'mavlink_settings.html' },
            { id: 'network', icon: 'wifi', src: 'connect.html' }
        ]},
        { sectionKey: 'device', items: [
            { id: 'hardware', icon: 'cpu', src: 'settings.html?v=gps-fix-1' }
        ]},
        { sectionKey: 'security', items: [
            { id: 'tokens', icon: 'shield', src: 'tokens.html' }
        ]},
        { sectionKey: 'system', items: [
            { id: 'settings', icon: 'settings', panel: 'settings' }
        ]}
    ];

    var THEMES = ['light', 'sky', 'sand', 'midnight', 'graphite'];
    var FONT_SIZES = ['sm', 'md', 'lg'];

    var ICONS = {
        radio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/></svg>',
        sliders: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
        plug: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></svg>',
        wifi: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>',
        cpu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>',
        shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
        settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
    };

    var lang = localStorage.getItem(STORAGE_LANG) || 'vi';
    var theme = localStorage.getItem(STORAGE_THEME) || DEFAULT_THEME;
    if (LEGACY_THEMES[theme]) theme = LEGACY_THEMES[theme];
    var fontSize = localStorage.getItem(STORAGE_FONT) || 'md';
    var currentView = DEFAULT_VIEW;
    var sidebarEl, backdropEl, frames = {};
    var lastStatus = null;

    function t(key) {
        var parts = key.split('.');
        var obj = I18N[lang];
        for (var i = 0; i < parts.length; i++) {
            if (!obj) return key;
            obj = obj[parts[i]];
        }
        return obj || key;
    }

    function allNavItems() {
        var items = [];
        NAV_STRUCTURE.forEach(function (g) { items = items.concat(g.items); });
        return items;
    }

    function isValidView(id) {
        return allNavItems().some(function (item) { return item.id === id; });
    }

    function findNavItem(viewId) {
        var found = null;
        NAV_STRUCTURE.forEach(function (g) {
            g.items.forEach(function (item) {
                if (item.id === viewId) found = item;
            });
        });
        return found;
    }

    function applyTheme(name) {
        if (LEGACY_THEMES[name]) name = LEGACY_THEMES[name];
        if (THEMES.indexOf(name) < 0) name = DEFAULT_THEME;
        theme = name;
        document.documentElement.setAttribute('data-theme', name);
        localStorage.setItem(STORAGE_THEME, name);
        document.querySelectorAll('.theme-card').forEach(function (el) {
            el.classList.toggle('active', el.dataset.theme === name);
        });
        syncThemeToFrames(name);
    }

    function applyFontSize(size) {
        if (FONT_SIZES.indexOf(size) < 0) size = 'md';
        fontSize = size;
        document.documentElement.setAttribute('data-font', size);
        localStorage.setItem(STORAGE_FONT, size);
        document.querySelectorAll('#font-size-row .option-btn').forEach(function (el) {
            el.classList.toggle('active', el.dataset.font === size);
        });
    }

    function syncThemeToFrames(name) {
        Object.keys(frames).forEach(function (k) {
            var iframe = frames[k];
            if (!iframe || !iframe.contentWindow) return;
            try {
                iframe.contentWindow.postMessage({ type: 'db-theme', theme: name }, '*');
            } catch (e) { /* ignore */ }
        });
    }

    function buildThemeGrid() {
        var grid = document.getElementById('theme-grid');
        if (!grid) return;
        grid.innerHTML = THEMES.map(function (name) {
            return '<button type="button" class="theme-card' + (theme === name ? ' active' : '') + '" data-theme="' + name + '">'
                + '<div class="theme-preview"><div class="theme-preview-sidebar"></div>'
                + '<div class="theme-preview-main"><div class="theme-preview-bar"></div><div class="theme-preview-block"></div></div></div>'
                + '<span class="theme-card-name">' + t('themes.' + name) + '</span></button>';
        }).join('');
        grid.querySelectorAll('.theme-card').forEach(function (el) {
            el.addEventListener('click', function () { applyTheme(el.dataset.theme); });
        });
    }

    function setLang(next) {
        lang = next === 'en' ? 'en' : 'vi';
        localStorage.setItem(STORAGE_LANG, lang);
        document.documentElement.lang = lang;
        document.getElementById('lang-vi').classList.toggle('active', lang === 'vi');
        document.getElementById('lang-en').classList.toggle('active', lang === 'en');
        applyStaticText();
        buildThemeGrid();
        buildNav();
        if (lastStatus) updateStatusPills(lastStatus);
        if (currentView !== 'settings') {
            document.title = 'UAVLink-Edge — ' + t('nav.' + currentView);
        } else {
            document.title = 'UAVLink-Edge — ' + t('settings.title');
        }
    }

    function applyStaticText() {
        document.querySelectorAll('[data-i18n]').forEach(function (el) {
            el.textContent = t(el.dataset.i18n);
        });
        document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            el.title = t(el.dataset.i18nTitle);
        });
    }

    function buildNav() {
        var nav = document.getElementById('sidebar-nav');
        if (!nav) return;
        var html = '';
        NAV_STRUCTURE.forEach(function (group) {
            html += '<div class="nav-section-label">' + t('sections.' + group.sectionKey) + '</div>';
            group.items.forEach(function (item) {
                var label = item.id === 'settings' ? t('nav.settings') : t('nav.' + item.id);
                html += '<button class="nav-item' + (currentView === item.id ? ' active' : '') + '" data-view="' + item.id + '">'
                    + (ICONS[item.icon] || '') + '<span>' + label + '</span></button>';
            });
        });
        nav.innerHTML = html;
        nav.querySelectorAll('.nav-item').forEach(function (btn) {
            btn.addEventListener('click', function () { navigate(btn.dataset.view); });
        });
    }

    function navigate(viewId, pushHash) {
        if (!isValidView(viewId)) viewId = DEFAULT_VIEW;
        currentView = viewId;

        document.querySelectorAll('.nav-item').forEach(function (el) {
            el.classList.toggle('active', el.dataset.view === viewId);
        });

        var isSettings = viewId === 'settings';
        document.getElementById('panel-settings').classList.toggle('active', isSettings);
        document.getElementById('panel-frame').classList.toggle('hidden', isSettings);

        if (isSettings) {
            document.title = 'UAVLink-Edge — ' + t('settings.title');
            Object.keys(frames).forEach(function (k) {
                frames[k].classList.remove('active');
            });
        } else {
            document.title = 'UAVLink-Edge — ' + t('nav.' + viewId);
            var frame = ensureFrame(viewId);
            if (frame) {
                frame.classList.add('active');
                Object.keys(frames).forEach(function (k) {
                    if (k !== viewId) frames[k].classList.remove('active');
                });
            }
        }

        if (pushHash !== false) {
            history.replaceState(null, '', '#/' + viewId);
        }
        closeMobileSidebar();
    }

    function ensureFrame(viewId) {
        if (frames[viewId]) return frames[viewId];
        var item = findNavItem(viewId);
        if (!item || !item.src) return null;

        var iframe = document.createElement('iframe');
        iframe.className = 'content-frame';
        iframe.title = t('nav.' + viewId);
        iframe.src = item.src + (item.src.indexOf('?') >= 0 ? '&' : '?') + 'embed=1&shellTheme=' + encodeURIComponent(theme);
        iframe.addEventListener('load', function () { syncThemeToFrames(theme); });
        iframe.setAttribute('loading', 'lazy');
        document.getElementById('frame-container').appendChild(iframe);
        frames[viewId] = iframe;
        return iframe;
    }

    function parseHash() {
        var hash = location.hash.replace(/^#\/?/, '');
        if (hash === 'overview') return DEFAULT_VIEW;
        return isValidView(hash) ? hash : DEFAULT_VIEW;
    }

    function setPill(id, value, state) {
        var el = document.getElementById(id);
        if (!el) return;
        var valEl = el.querySelector('.value');
        if (valEl) valEl.textContent = value;
        el.className = 'status-pill' + (state ? ' ' + state : '');
        var dot = el.querySelector('.status-dot');
        if (dot) dot.className = 'status-dot ' + (state || '');
    }

    function updateStatusPills(data) {
        lastStatus = data;
        var v = I18N[lang].val;
        var authOk = data.server_reachable || data.auth_status === 'Authenticated';
        setPill('pill-system', v.online, 'ok');
        setPill('pill-auth', data.auth_status === 'Reconnecting' ? v.reconnecting : (authOk ? 'OK' : (data.auth_status || 'N/A')), authOk ? 'ok' : 'err');
        var px4Text = data.pixhawk_connected && data.telemetry_valid ? (data.flight_mode || v.connected) : (data.pixhawk_connected ? v.heartbeat : v.offline);
        setPill('pill-px4', px4Text, data.pixhawk_connected && data.telemetry_valid ? 'ok' : (data.pixhawk_connected ? 'warn' : 'err'));
        setPill('pill-network', data.network_type || v.unknown, data.network_type === 'WiFi' || data.network_type === 'Ethernet' ? 'ok' : (data.network_type === '4G/LTE' ? 'warn' : ''));
        setPill('pill-ip', data.current_ip || 'N/A', '');
        setPill('pill-uptime', data.uptime ? data.uptime.split('.')[0] : 'N/A', '');
        var msgRate = Number(data.mavlink_uplink_msg_per_sec || data.udp_msg_per_sec || 0);
        setPill('pill-mavlink', msgRate > 0 ? msgRate.toFixed(0) + ' msg/s' : v.idle, msgRate > 0 ? 'ok' : 'err');
    }

    function pollStatus() {
        fetch('/api/status')
            .then(function (r) { return r.json(); })
            .then(updateStatusPills)
            .catch(function () { setPill('pill-system', I18N[lang].val.offline, 'err'); });
    }

    function closeMobileSidebar() {
        if (sidebarEl) sidebarEl.classList.remove('mobile-open');
        if (backdropEl) backdropEl.classList.remove('visible');
    }

    function initSettings() {
        document.getElementById('lang-vi').addEventListener('click', function () { setLang('vi'); });
        document.getElementById('lang-en').addEventListener('click', function () { setLang('en'); });
        document.querySelectorAll('#font-size-row .option-btn').forEach(function (el) {
            el.addEventListener('click', function () { applyFontSize(el.dataset.font); });
        });
        applyTheme(theme);
        applyFontSize(fontSize);
        buildThemeGrid();
        setLang(lang);
    }

    function initSidebar() {
        sidebarEl = document.getElementById('app-sidebar');
        backdropEl = document.getElementById('sidebar-backdrop');
        document.getElementById('sidebar-toggle').addEventListener('click', function () {
            sidebarEl.classList.toggle('collapsed');
        });
        document.getElementById('mobile-menu-btn').addEventListener('click', function () {
            sidebarEl.classList.toggle('mobile-open');
            backdropEl.classList.toggle('visible');
        });
        backdropEl.addEventListener('click', closeMobileSidebar);
        window.addEventListener('hashchange', function () { navigate(parseHash(), false); });
    }

    function preloadFrames() {
        NAV_STRUCTURE.forEach(function (group) {
            group.items.forEach(function (item) {
                if (item.src) ensureFrame(item.id);
            });
        });
        if (currentView !== 'settings') {
            Object.keys(frames).forEach(function (k) {
                frames[k].classList.toggle('active', k === currentView);
            });
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        initSettings();
        initSidebar();
        navigate(parseHash(), false);
        preloadFrames();
        pollStatus();
        setInterval(pollStatus, 2000);
    });
})();
