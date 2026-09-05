// ========================================
// تنظیمات
// ========================================

// آدرس API رباتت را اینجا قرار بده
const API_URL = "https://api.render.com/deploy/srv-dae3kt740ujc73dm3ljg?key=_pk18dbgRvQ";


// ========================================
// TELEGRAM
// ========================================

const tg = window.Telegram.WebApp;

tg.ready();
tg.expand();


// کاربر فعلی تلگرام
const telegramUser = tg.initDataUnsafe?.user;

const userId = telegramUser?.id;


// ========================================
// ELEMENTS
// ========================================

const walletElement = document.getElementById("wallet");
const stockNameElement = document.getElementById("stockName");
const priceElement = document.getElementById("price");
const changeElement = document.getElementById("change");
const sharesElement = document.getElementById("shares");
const portfolioValueElement =
    document.getElementById("portfolioValue");

const canvas = document.getElementById("chart");
const ctx = canvas.getContext("2d");


// ========================================
// DATA
// ========================================

let currentPrice = 0;
let currentShares = 0;

let chartPrices = [];


// ========================================
// TOAST
// ========================================

function toast(message) {

    const element = document.getElementById("toast");

    element.textContent = message;
    element.classList.add("show");

    setTimeout(() => {
        element.classList.remove("show");
    }, 2500);
}


// ========================================
// NUMBER FORMAT
// ========================================

function formatNumber(number) {

    return Number(number || 0).toLocaleString("fa-IR");
}


// ========================================
// WALLET
// ========================================

async function loadWallet() {

    if (!userId) {

        walletElement.textContent = "نامشخص";

        return;
    }

    try {

        const response = await fetch(
            `${API_URL}/wallet/${userId}`
        );

        if (!response.ok) {
            throw new Error("Wallet request failed");
        }

        const data = await response.json();

        walletElement.textContent =
            formatNumber(data.coins);

    } catch (error) {

        console.error(error);

        walletElement.textContent = "خطا";
    }
}


// ========================================
// MARKET
// ========================================

async function loadMarket() {

    try {

        const response = await fetch(
            `${API_URL}/market`
        );

        if (!response.ok) {
            throw new Error("Market request failed");
        }

        const data = await response.json();

        currentPrice = Number(data.price || 0);

        stockNameElement.textContent =
            data.name || "BETA";

        priceElement.textContent =
            formatNumber(currentPrice);


        // تغییر قیمت

        let change =
            data.change ?? data.change_percent ?? 0;

        change = Number(change);

        if (change > 0) {

            changeElement.textContent =
                `▲ +${change}%`;

        } else if (change < 0) {

            changeElement.textContent =
                `▼ ${change}%`;

        } else {

            changeElement.textContent =
                "بدون تغییر";
        }


        // قیمت برای نمودار

        chartPrices.push(currentPrice);

        if (chartPrices.length > 30) {
            chartPrices.shift();
        }

        drawChart();

    } catch (error) {

        console.error(error);

        priceElement.textContent = "خطا";
        changeElement.textContent =
            "اتصال به بازار برقرار نشد";
    }
}


// ========================================
// PORTFOLIO
// ========================================

async function loadPortfolio() {

    if (!userId) {
        return;
    }

    try {

        const response = await fetch(
            `${API_URL}/portfolio/${userId}`
        );

        if (!response.ok) {
            return;
        }

        const data = await response.json();

        currentShares =
            Number(data.shares || 0);

        sharesElement.textContent =
            formatNumber(currentShares);

        portfolioValueElement.textContent =
            formatNumber(
                currentShares * currentPrice
            );

    } catch (error) {

        console.log(
            "Portfolio هنوز در API ساخته نشده."
        );
    }
}


// ========================================
// CHART
// ========================================

function drawChart() {

    const width = canvas.clientWidth;
    const height = canvas.clientHeight;

    const dpr = window.devicePixelRatio || 1;

    canvas.width = width * dpr;
    canvas.height = height * dpr;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    ctx.clearRect(
        0,
        0,
        width,
        height
    );


    if (chartPrices.length < 2) {
        return;
    }


    const min =
        Math.min(...chartPrices);

    const max =
        Math.max(...chartPrices);

    const range =
        max - min || 1;


    ctx.beginPath();


    chartPrices.forEach((value, index) => {

        const x =
            (index / (chartPrices.length - 1))
            * width;

        const y =
            height -
            ((value - min) / range)
            * (height - 15)
            - 5;


        if (index === 0) {
            ctx.moveTo(x, y);
        } else {
            ctx.lineTo(x, y);
        }

    });


    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;

    ctx.stroke();
}


// ========================================
// BUY
// ========================================

async function buyStock() {

    if (!userId) {

        toast("کاربر تلگرام پیدا نشد");

        return;
    }

    const amount =
        Number(
            document.getElementById("amount").value
        );


    if (!amount || amount <= 0) {

        toast("تعداد نامعتبر است");

        return;
    }


    try {

        const response = await fetch(
            `${API_URL}/buy`,
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    user_id: userId,
                    amount: amount
                })
            }
        );


        const data = await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "خرید انجام نشد"
            );

            return;
        }


        toast("خرید با موفقیت انجام شد ✅");

        await loadWallet();
        await loadPortfolio();

    } catch (error) {

        console.error(error);

        toast("خطا در اتصال به سرور");
    }
}


// ========================================
// SELL
// ========================================

async function sellStock() {

    if (!userId) {

        toast("کاربر تلگرام پیدا نشد");

        return;
    }


    const amount =
        Number(
            document.getElementById("amount").value
        );


    if (!amount || amount <= 0) {

        toast("تعداد نامعتبر است");

        return;
    }


    try {

        const response = await fetch(
            `${API_URL}/sell`,
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    user_id: userId,
                    amount: amount
                })
            }
        );


        const data = await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "فروش انجام نشد"
            );

            return;
        }


        toast("فروش با موفقیت انجام شد ✅");

        await loadWallet();
        await loadPortfolio();

    } catch (error) {

        console.error(error);

        toast("خطا در اتصال به سرور");
    }
}


// ========================================
// ADMIN PRICE
// ========================================

async function changePrice() {

    const price =
        Number(
            document.getElementById("adminPrice").value
        );


    if (!price || price <= 0) {

        toast("قیمت نامعتبر است");

        return;
    }


    try {

        const response = await fetch(
            `${API_URL}/admin/price`,
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    user_id: userId,
                    price: price
                })
            }
        );


        const data = await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "تغییر قیمت انجام نشد"
            );

            return;
        }


        toast("قیمت تغییر کرد ✅");

        await loadMarket();

    } catch (error) {

        toast("خطا در اتصال به سرور");
    }
}


// ========================================
// ADMIN RANDOM SETTINGS
// ========================================

async function changeRandomSettings() {

    const minChange =
        Number(
            document.getElementById("minChange").value
        );

    const maxChange =
        Number(
            document.getElementById("maxChange").value
        );


    try {

        const response = await fetch(
            `${API_URL}/admin/random`,
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    user_id: userId,
                    min_change: minChange,
                    max_change: maxChange,
                    random_enabled: true
                })
            }
        );


        const data = await response.json();


        if (!response.ok) {

            toast(
                data.error ||
                "تنظیمات ذخیره نشد"
            );

            return;
        }


        toast("تنظیمات ذخیره شد ✅");

    } catch (error) {

        toast("خطا در اتصال به سرور");
    }
}


// ========================================
// LOAD EVERYTHING
// ========================================

async function loadAll() {

    await loadWallet();

    await loadMarket();

    await loadPortfolio();
}


loadAll();


// ========================================
// AUTO REFRESH
// ========================================

setInterval(() => {

    loadWallet();
    loadMarket();
    loadPortfolio();

}, 10000);


// ========================================
// RESIZE
// ========================================

window.addEventListener(
    "resize",
    drawChart
);