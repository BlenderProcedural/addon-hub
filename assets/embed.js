// Embed helpers for when the page is shown inside an iframe (e.g. a store's product description).
// Links to other pages open in a new tab instead of inside the frame; in-page anchors stay in the frame.
// The page height is posted to the parent so a host that listens can size the frame.
(function () {
  if (window.self === window.top) return;
  document.documentElement.classList.add('framed');
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a');
    if (!a || (a.getAttribute('href') || '').charAt(0) === '#') return;
    a.target = '_blank';
    a.rel = 'noopener';
  });
  function post() {
    try { parent.postMessage({ type: 'addon-hub-height', height: document.documentElement.scrollHeight }, '*'); } catch (err) { /* ignore */ }
  }
  window.addEventListener('load', post);
  if (window.ResizeObserver) new ResizeObserver(post).observe(document.body);
})();
