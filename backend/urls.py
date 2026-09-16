from django.urls import path

from backend import auth_views as auth
from backend import catalog_views as catalog
from backend import order_views as orders

app_name = "backend"
urlpatterns = [
    path("user/register", auth.RegisterAccount.as_view(), name="user-register"),
    path("user/register/resend", auth.ResendConfirmation.as_view(), name="user-register-resend"),
    path("user/register/confirm", auth.ConfirmAccount.as_view(), name="user-register-confirm"),
    path("user/login", auth.LoginAccount.as_view(), name="user-login"),
    path("user/logout", auth.LogoutAccount.as_view(), name="user-logout"),
    path("user/details", auth.AccountDetails.as_view(), name="user-details"),
    path("user/password_reset", auth.PasswordReset.as_view(), name="password-reset"),
    path("user/password_reset/confirm", auth.PasswordResetConfirm.as_view(),
         name="password-reset-confirm"),
    path("user/contact", orders.ContactView.as_view(), name="user-contact"),
    path("categories", catalog.CategoryView.as_view(), name="categories"),
    path("shops", catalog.ShopView.as_view(), name="shops"),
    path("products", catalog.ProductInfoView.as_view(), name="products"),
    path("products/<int:pk>", catalog.ProductDetailView.as_view(), name="product-detail"),
    path("partner/update", catalog.PartnerUpdate.as_view(), name="partner-update"),
    path("partner/state", catalog.PartnerState.as_view(), name="partner-state"),
    path("partner/export", catalog.PartnerExport.as_view(), name="partner-export"),
    path("partner/jobs/<int:pk>", catalog.CatalogJobView.as_view(), name="partner-job"),
    path("partner/orders", orders.PartnerOrders.as_view(), name="partner-orders"),
    path("basket", orders.BasketView.as_view(), name="basket"),
    path("order", orders.OrderView.as_view(), name="order"),
    path("order/<int:pk>", orders.OrderDetailView.as_view(), name="order-detail"),
    path("admin/orders", orders.AdminOrderView.as_view(), name="admin-orders"),
    path("admin/orders/<int:pk>/status", orders.OrderStatusView.as_view(), name="order-status"),
]
