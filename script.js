const animateValue = (id, target, suffix = '', speed = 22) => {
  const el = document.getElementById(id);
  if (!el) return;
  let value = 0;
  const step = Math.max(1, Math.floor(target / 90));
  const timer = setInterval(() => {
    value += step;
    if (value >= target) {
      value = target;
      clearInterval(timer);
    }
    el.textContent = `${value.toLocaleString()}${suffix}`;
  }, speed);
};

window.addEventListener('load', () => {
  animateValue('sales', 482);
  animateValue('subs', 7630);
  animateValue('drops', 112);
  animateValue('conv', 18, '%');
});
