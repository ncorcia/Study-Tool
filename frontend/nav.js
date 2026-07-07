(function () {
  var path = window.location.pathname;
  document.querySelectorAll('.main-nav-links a').forEach(function (a) {
    var href = a.getAttribute('href');
    var isIndex = href === '/' && (path === '/' || path === '/index.html');
    if (isIndex || (href !== '/' && path === href)) a.classList.add('active');
  });

  fetch('/api/auth/me').then(function (r) {
    return r.ok ? r.json() : null;
  }).then(function (user) {
    var emailEl = document.getElementById('current-user-email');
    if (user && emailEl) emailEl.textContent = user.email;
  });

  var logoutLink = document.getElementById('logout-link');
  if (logoutLink) {
    logoutLink.addEventListener('click', function (e) {
      e.preventDefault();
      fetch('/api/auth/logout', { method: 'POST' }).then(function () {
        window.location.href = '/login.html';
      });
    });
  }
})();
