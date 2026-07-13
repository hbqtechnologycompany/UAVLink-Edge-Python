(function () {
    if (new URLSearchParams(window.location.search).get('embed') !== '1') return;

    document.documentElement.classList.add('db-embed');

    var LEGACY_THEMES = {
        ocean: 'sky', forest: 'sand', purple: 'graphite', carbon: 'midnight', neon: 'graphite', indigo: 'graphite'
    };

    function applyShellTheme(name) {
        if (LEGACY_THEMES[name]) name = LEGACY_THEMES[name];
        var allowed = ['light', 'sky', 'sand', 'midnight', 'graphite'];
        if (allowed.indexOf(name) < 0) name = 'light';
        document.documentElement.setAttribute('data-theme', name);
    }

    var themeParam = new URLSearchParams(window.location.search).get('shellTheme');
    if (themeParam) applyShellTheme(themeParam);

    var scrollPages = ['mavlink.html', 'connect.html', 'mavlink_settings.html', 'settings.html'];
    var page = window.location.pathname.split('/').pop() || '';
    if (scrollPages.some(function (p) { return page.indexOf(p.replace('.html', '')) >= 0; })) {
        document.documentElement.classList.add('db-scroll');
    }

    window.addEventListener('message', function (e) {
        if (e.data && e.data.type === 'db-theme' && e.data.theme) {
            applyShellTheme(e.data.theme);
        }
    });

    document.addEventListener('DOMContentLoaded', function () {
        document.body.classList.add('db-embed');
    });
})();
